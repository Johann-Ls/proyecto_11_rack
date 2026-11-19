# main.py — Proyecto 11: Monitoreo de cuarto de comunicaciones
# Raspberry Pi Pico 2 W · MicroPython · asyncio · Adafruit IO (MQTT)
#
# Archivos que deben estar en la Pico:
#   main.py          (este archivo)
#   umqtt_simple.py  (librería MQTT, sin modificar)
#   secretos.py      (Wi-Fi y Key de Adafruit; NO se sube a GitHub)

import asyncio
import time
import network
from machine import Pin, I2C, PWM
from umqtt_simple import MQTTClient
from secretos import WIFI_SSID, WIFI_CLAVE, AIO_USER, AIO_KEY


# ============================== AJUSTES ==============================
# Pines (cámbialos si tu conexión es distinta)
PIN_SDA = 4            # SHT31 SDA  (I2C0)
PIN_SCL = 5            # SHT31 SCL  (I2C0)
PIN_AGUA = 15          # salida digital del sensor de fuga
PIN_PUERTA = 14        # sensor magnético (reed switch) a GND
PIN_VENT = 16          # PWM hacia el MOSFET de los ventiladores de 12 V
PIN_LED_ALERTA = 17    # LED rojo de alerta
PIN_BUZZER = 18        # buzzer activo

# Lógica de los sensores digitales
AGUA_ACTIVO = 0        # valor que da el sensor de fuga cuando está mojado
PUERTA_ABIERTA = 1     # con pull-up: imán cerca = 0 (cerrada), imán lejos = 1 (abierta)

# Temperaturas y ventiladores
TEMP_MAX = 30.0        # °C: por encima se genera la alerta "Temperatura alta"
TEMP_VENT_MIN = 24.0   # °C: por debajo, ventiladores apagados (modo AUTO)
TEMP_VENT_MAX = 32.0   # °C: desde aquí, ventiladores al 100 % (modo AUTO)
VENT_MIN = 30          # % mínimo para que el ventilador arranque
PWM_FREQ = 1000        # Hz
PROTECCION_TERMICA = True   # en modo OFF, si la temperatura supera TEMP_MAX, fuerza 100 %

# Tiempos
INTERVALO_SENSOR_MS = 2000
INTERVALO_NUBE_MS = 15000   # Adafruit IO gratis: máximo 30 datos por minuto
ESPERA_RECONEXION_S = 10    # Adafruit IO: máximo 20 intentos de conexión por minuto

# Adafruit IO
AIO_SERVIDOR = "io.adafruit.com"
CLIENT_ID = b"pico-rack-g11"


# ============================== HARDWARE ==============================
i2c = I2C(0, sda=Pin(PIN_SDA), scl=Pin(PIN_SCL), freq=100000)
pin_agua = Pin(PIN_AGUA, Pin.IN, Pin.PULL_UP)
pin_puerta = Pin(PIN_PUERTA, Pin.IN, Pin.PULL_UP)

pwm_vent = PWM(Pin(PIN_VENT))
pwm_vent.freq(PWM_FREQ)
pwm_vent.duty_u16(0)

led_alerta = Pin(PIN_LED_ALERTA, Pin.OUT, value=0)
buzzer = Pin(PIN_BUZZER, Pin.OUT, value=0)
led_placa = Pin("LED", Pin.OUT)


# ============================== ESTADO ==============================
temperatura = None
humedad = None
sensor_ok = False
agua = 0                   # 1 = fuga detectada
puerta = 0                 # 1 = puerta abierta
velocidad = 0              # % de los ventiladores
modo_ventiladores = "AUTO" # AUTO, ON u OFF (lo cambia el dashboard)
nube_ok = False


# ============================== SENSOR SHT31 ==============================
SHT31_ADDR = 0x44

def _crc8(datos):
    crc = 0xFF
    for b in datos:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ 0x31) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc

async def leer_sht31():
    i2c.writeto(SHT31_ADDR, b"\x24\x00")      # medición de alta repetibilidad
    await asyncio.sleep_ms(20)                # espera sin bloquear las otras tareas
    d = i2c.readfrom(SHT31_ADDR, 6)
    if _crc8(d[0:2]) != d[2] or _crc8(d[3:5]) != d[5]:
        raise ValueError("CRC inválido")
    t_raw = (d[0] << 8) | d[1]
    h_raw = (d[3] << 8) | d[4]
    t = -45 + 175 * t_raw / 65535
    h = 100 * h_raw / 65535
    return t, h

async def tarea_sensor():
    global temperatura, humedad, sensor_ok
    fallos = 0
    while True:
        try:
            temperatura, humedad = await leer_sht31()
            if not sensor_ok:
                print("SHT31 funcionando")
            sensor_ok = True
            fallos = 0
        except Exception as e:
            fallos += 1
            if fallos == 3:
                sensor_ok = False
                print("SHT31 sin respuesta:", e)
        await asyncio.sleep_ms(INTERVALO_SENSOR_MS)


# ============================== SENSORES DIGITALES ==============================
async def tarea_digitales():
    # Lee agua y puerta cada 100 ms; un cambio cuenta solo si se repite 3 veces (antirrebote)
    global agua, puerta
    cont_agua = 0
    cont_puerta = 0
    while True:
        a = 1 if pin_agua.value() == AGUA_ACTIVO else 0
        p = 1 if pin_puerta.value() == PUERTA_ABIERTA else 0

        if a != agua:
            cont_agua += 1
            if cont_agua >= 3:
                agua = a
                cont_agua = 0
                print("Agua:", "FUGA DETECTADA" if agua else "seco")
        else:
            cont_agua = 0

        if p != puerta:
            cont_puerta += 1
            if cont_puerta >= 3:
                puerta = p
                cont_puerta = 0
                print("Puerta:", "ABIERTA" if puerta else "cerrada")
        else:
            cont_puerta = 0

        await asyncio.sleep_ms(100)


# ============================== CONTROL DE VENTILADORES ==============================
def velocidad_auto(t):
    if t is None:
        return 100                      # sin lectura del sensor: por seguridad, al máximo
    if t < TEMP_VENT_MIN:
        return 0
    if t >= TEMP_VENT_MAX:
        return 100
    return int(VENT_MIN + (t - TEMP_VENT_MIN) * (100 - VENT_MIN) / (TEMP_VENT_MAX - TEMP_VENT_MIN))

async def tarea_control():
    global velocidad
    while True:
        t = temperatura if sensor_ok else None
        if modo_ventiladores == "ON":
            v = 100
        elif modo_ventiladores == "OFF":
            v = 0
            if PROTECCION_TERMICA and t is not None and t > TEMP_MAX:
                v = 100
        else:
            v = velocidad_auto(t)

        if v != velocidad:
            print("Ventiladores: {}% (modo {})".format(v, modo_ventiladores))
        velocidad = v
        pwm_vent.duty_u16(v * 65535 // 100)
        await asyncio.sleep(1)


# ============================== ALERTAS LOCALES ==============================
async def tarea_alertas():
    while True:
        temp_alta = sensor_ok and temperatura > TEMP_MAX
        if agua:
            # Fuga de agua: la alerta más grave, LED y buzzer rápidos
            led_alerta.on(); buzzer.on()
            await asyncio.sleep_ms(150)
            led_alerta.off(); buzzer.off()
            await asyncio.sleep_ms(150)
        elif temp_alta or puerta or not sensor_ok:
            # Otras alertas: solo el LED parpadea
            buzzer.off()
            led_alerta.toggle()
            await asyncio.sleep_ms(500)
        else:
            led_alerta.off(); buzzer.off()
            await asyncio.sleep_ms(500)


# ============================== NUBE (Wi-Fi + Adafruit IO) ==============================
wlan = network.WLAN(network.STA_IF)
cliente = None

def tema(feed):
    return (AIO_USER + "/feeds/" + feed).encode()

async def conectar_wifi():
    if wlan.isconnected():
        return True
    print("Conectando a Wi-Fi '{}'...".format(WIFI_SSID))
    wlan.active(True)
    try:
        wlan.config(pm=0xa11140)        # desactiva el ahorro de energía: conexión más estable
    except Exception:
        pass
    try:
        wlan.disconnect()
    except Exception:
        pass
    wlan.connect(WIFI_SSID, WIFI_CLAVE)
    for _ in range(20):                 # hasta ~10 s, sin bloquear las otras tareas
        if wlan.isconnected():
            print("Wi-Fi conectado, IP:", wlan.ifconfig()[0])
            return True
        await asyncio.sleep_ms(500)
    print("Wi-Fi: no se pudo conectar")
    return False

def al_recibir(topic, msg):
    # Acción remota: el dashboard envía AUTO, ON u OFF por cmd-ventiladores
    global modo_ventiladores
    valor = msg.decode().strip().upper()
    if valor in ("AUTO", "ON", "OFF"):
        modo_ventiladores = valor
        print("Comando del dashboard:", valor)

def cerrar_mqtt():
    global cliente
    if cliente:
        try:
            cliente.sock.close()
        except Exception:
            pass
    cliente = None

def conectar_mqtt():
    global cliente
    cerrar_mqtt()
    cliente = MQTTClient(CLIENT_ID, AIO_SERVIDOR, user=AIO_USER, password=AIO_KEY, keepalive=60)
    cliente.set_last_will(tema("estado"), b"offline")    # si la Pico se cae, Adafruit pone "offline"
    cliente.set_callback(al_recibir)
    cliente.connect(timeout=10)
    cliente.sock.settimeout(5)
    cliente.subscribe(tema("cmd-ventiladores"))
    cliente.publish(tema("estado"), b"online")
    print("Conectado a Adafruit IO")

def revisar_comandos():
    cliente.check_msg()
    cliente.sock.settimeout(5)          # check_msg deja el socket sin límite de espera

async def tarea_nube():
    global nube_ok
    ultimo_envio = time.ticks_add(time.ticks_ms(), -INTERVALO_NUBE_MS)  # primer envío inmediato
    prev_agua = None
    prev_puerta = None
    prev_temp_alta = False
    prev_sensor_ok = True

    while True:
        try:
            # 1) Reconexión: primero Wi-Fi, luego MQTT
            if not wlan.isconnected():
                if nube_ok:
                    print("Se perdió el Wi-Fi")
                nube_ok = False
                if not await conectar_wifi():
                    await asyncio.sleep(ESPERA_RECONEXION_S)
                    continue
            if not nube_ok:
                conectar_mqtt()
                nube_ok = True
                prev_agua = None        # al reconectar, se reenvía el estado actual
                prev_puerta = None

            # 2) Recibir comandos del dashboard
            revisar_comandos()

            # 3) Cada 15 s: temperatura, humedad y ventiladores
            ahora = time.ticks_ms()
            if time.ticks_diff(ahora, ultimo_envio) >= INTERVALO_NUBE_MS:
                if sensor_ok:
                    cliente.publish(tema("temp"), "{:.1f}".format(temperatura))
                    cliente.publish(tema("hum"), "{:.0f}".format(humedad))
                cliente.publish(tema("ventiladores"), str(velocidad))
                ultimo_envio = ahora

            # 4) Solo cuando cambian: agua, puerta y alertas
            if agua != prev_agua:
                cliente.publish(tema("agua"), b"1" if agua else b"0")
                if agua:
                    cliente.publish(tema("alerta"), b"Fuga de agua")
                prev_agua = agua

            if puerta != prev_puerta:
                cliente.publish(tema("puerta"), b"1" if puerta else b"0")
                if puerta:
                    cliente.publish(tema("alerta"), b"Puerta abierta")
                prev_puerta = puerta

            temp_alta = sensor_ok and temperatura > TEMP_MAX
            if temp_alta and not prev_temp_alta:
                cliente.publish(tema("alerta"), b"Temperatura alta")
            prev_temp_alta = temp_alta

            if not sensor_ok and prev_sensor_ok:
                cliente.publish(tema("alerta"), b"Falla del sensor de temperatura")
            prev_sensor_ok = sensor_ok

        except Exception as e:
            print("Nube: error ({}). Reintento en {} s".format(e, ESPERA_RECONEXION_S))
            nube_ok = False
            cerrar_mqtt()
            await asyncio.sleep(ESPERA_RECONEXION_S)
            continue

        await asyncio.sleep_ms(500)


# ============================== MONITOREO EN THONNY ==============================
async def tarea_latido():
    # LED de la placa: parpadeo lento = conectado a la nube; rápido = sin conexión
    while True:
        led_placa.toggle()
        await asyncio.sleep_ms(1000 if nube_ok else 200)

async def tarea_monitor():
    while True:
        if sensor_ok:
            th = "{:.1f} °C  {:.0f} %".format(temperatura, humedad)
        else:
            th = "sensor sin datos"
        print("[{}] {} | agua:{} puerta:{} | vent:{}% {} | nube:{}".format(
            time.ticks_ms() // 1000, th, agua, puerta, velocidad, modo_ventiladores,
            "OK" if nube_ok else "--"))
        await asyncio.sleep(5)


# ============================== PROGRAMA PRINCIPAL ==============================
async def main():
    print("Proyecto 11 · Monitoreo de cuarto de comunicaciones")
    await asyncio.gather(
        tarea_sensor(),
        tarea_digitales(),
        tarea_control(),
        tarea_alertas(),
        tarea_nube(),
        tarea_latido(),
        tarea_monitor(),
    )

try:
    asyncio.run(main())
finally:
    # Al detener el programa (Ctrl+C en Thonny), deja todo apagado
    pwm_vent.duty_u16(0)
    buzzer.off()
    led_alerta.off()
    cerrar_mqtt()
    asyncio.new_event_loop()
