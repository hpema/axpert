#! /usr/bin/python3

import usb.core, usb.util, usb.control
import crc16
from datetime import datetime
import re
import time
import paho.mqtt.client as mqtt
import simplejson as json
import logging


# <settings>
mqtt_host = "local-mqtt-server"
mqtt_port = 1883
mqtt_user = "pubsubclient"
mqtt_pass = "beI3WG45yv"
mqtt_topic_sub = "axpert/comm"
mqtt_topic_pub = "axpert/info"

# minimum 2 seconds due to speed reading data from inverter
polling_delay = 2 

log_file = "/etc/axpert/inverter.log"
data_file = "/etc/axpert/readings.json"
# </settings>




g_last_pvw = 0
g_last_outw = 0
g_last_pigs = datetime.now()
g_last_days = datetime.now().day
g_last_mins = datetime.now().minute
g_json_data = { "version": "1", "day": g_last_days }
g_json_outw = {}
g_json_pvw = {}
# generate default values
for x in range(24): 
  g_json_pvw[str(x)] = 0
  g_json_outw[str(x)] = 0

try:
  f = open(data_file)
  g_json_data = json.load(f)
  g_json_pvw = g_json_data["pvw"]
  g_json_outw = g_json_data["outw"]
  if "day" in g_json_data:
    g_last_days = g_json_data["day"]
  if "total" in g_json_pvw:
    g_json_pvw.pop("total")
  if "total" in g_json_outw:
    g_json_outw.pop("total")
except:
  print("Error reading json data file")


g_mqttcmd = ""
g_cmdcnt = -1
output_source = {0: "Grid", 1: "Solar", 2: "SBU"}
charge_source = {0: "Grid First", 1: "Solar First", 2: "Solar + Grid", 3: "Solar Only"}
qmod_values = {"L":"Grid", "B":"Battery", "F":"Fault", "P":"Power On", "S":"Standby", "H":"Power Saving"}


logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

# Uncomment below to use console logging
#handler = logging.StreamHandler()
#handler.setLevel(logging.DEBUG)
#formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S')
#handler.setFormatter(formatter)
#logger.addHandler(handler)

# Uncomment below to use logging to file
handler = logging.FileHandler(log_file, delay=True)
handler.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", '%Y/%m/%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

def on_mqtt_connect(client, userdata, flags, rc):
  if str(rc) == "0":
    mqttclient.subscribe(mqtt_topic_sub)
    mqttclient.publish(mqtt_topic_pub + "/status", "online", retain=True)

def on_mqtt_message(client, userdata, msg):
  global g_mqttcmd
  try:
    g_mqttcmd = msg.payload.decode("utf-8")
    logging.debug("Recieved command from MQTT: " + str(g_mqttcmd))
  except Exception as e:
    logging.error("MQTT on_message error: " + str(e))


def send_usb_data(command):
  cmd = command.encode('utf-8')
  crc = crc16.crc16xmodem(cmd).to_bytes(2,'big')
  cmd = cmd+crc+b'\r'
  while len(cmd)<8:
    cmd = cmd+b'\0'
  try:
    usbdev.ctrl_transfer(0x21, 0x9, 0x200, 0, cmd)
  except:
    pass
        

def read_usb_data(timeout=150):
  reply=""
  i=0
  while '\r' not in reply and i<20:
    try:
      reply+="".join([chr(i) for i in usbdev.read(0x81, 8, timeout) if i!=0x00])
    except:
      pass
    i+=1
  return reply


def execute_command(command):
  global g_cmdcnt
  global g_last_pvw
  global g_last_outw
  global g_last_pigs
  try:
    discarded = read_usb_data()
    send_usb_data(command)
    response = read_usb_data()
        
    if not isinstance(response, str):
      logging.warning("Inverter response is not a string. Command: " + command)

    elif len(response) < 3 or response[0] != '(':
      logging.warning("Invalid response received from inverter query: " + command + " Response was: " + response)

    elif command == "QMOD" and len(response) > 2:
      logging.debug(str(g_cmdcnt) + ' ' + command + ': ' + qmod_values.get(str(response[1])))
      mqttclient.publish(mqtt_topic_pub + "/mode", qmod_values.get(str(response[1]), "Unknown"))

    elif command == "QPIRI" and len(response) > 95:
      response = response[1:95]
      logging.debug(str(g_cmdcnt) + ' ' + command + ': ' + str(response))
      res_split = response.split(' ')
      json_data = { 
        "os": output_source.get(int(res_split[16]), "Unknown"),
        "cs": charge_source.get(int(res_split[17]), "Unknown")
      }
      mqttclient.publish(mqtt_topic_pub + "/piri", json.dumps(json_data))

    elif command == "QPIGS" and len(response) > 107:
      response = response[1:107]
      logging.debug(str(g_cmdcnt) + ' ' + command + ': ' + str(response))
      res_split = response.split(' ')
      json_data = { 
        "inV": float(res_split[0]),
        "inHz": float(res_split[1]),
        "outV": float(res_split[2]),
        "outHz": float(res_split[3]),
        "outVa": int(res_split[4]),
        "outW": int(res_split[5]),
        "outP": int(res_split[6]),
        "battAi": int(res_split[9]),
        "battAo": int(res_split[15]),
        "battV": float(res_split[8]),
        "battP": int(res_split[10]),
        "sccBattV": float(res_split[14]),
        "pvA": int(res_split[12]),
        "pvV": float(res_split[13]),
        "pvW": int(res_split[19]),
        "pvVa": round(float(res_split[13]) * int(res_split[12]), 1),
        "temp": int(res_split[11]),
        "on_solar": int(res_split[16][2]),
        "charging": int(res_split[16][5]),
        "charge_scc": int(res_split[16][6]),
        "charge_ac": int(res_split[16][7])
      }
      mqttclient.publish(mqtt_topic_pub + "/pigs", json.dumps(json_data))

      duration = (datetime.now() - g_last_pigs).total_seconds() / 3600
      if duration > 0:
        g_json_pvw[str(g_last_pigs.hour)] += (g_last_pvw * duration)
        g_json_outw[str(g_last_pigs.hour)] += (g_last_outw * duration)
      g_last_pigs = datetime.now()
      g_last_outw = json_data["outW"]
      g_last_pvw = json_data["pvW"]

    else:
      response = response[1:]
      logging.info(str(g_cmdcnt) + ' ' + command + ': ' + str(response))
            
  except Exception as e:
    logging.error("execute_command error: " + str(e))




if __name__ == '__main__':
  try:
    mqttclient = mqtt.Client("axpert-pi", False)
    mqttclient.on_connect = on_mqtt_connect
    mqttclient.on_message = on_mqtt_message
    mqttclient.username_pw_set(mqtt_user, mqtt_pass)
    mqttclient.reconnect_delay_set(1, 120)
    mqttclient.will_set(mqtt_topic_pub + "/status", "offline", retain=True)
    mqttclient.connect(mqtt_host, mqtt_port, 60)
    mqttclient.loop_start()
  except Exception as e:
    logging.critical("Error initialising MQTT: " + str(e))
    exit()

  try:
    usbdev = usb.core.find(idVendor=0x0665, idProduct=0x5161)
    if usbdev.is_kernel_driver_active(0): 
        usbdev.detach_kernel_driver(0)
    usbdev.set_interface_altsetting(0,0)
  except usb.core.USBError as e:
    logging.critical("Error initialising USB: " + str(e))
    exit()

  time.sleep(1)
  if polling_delay < 2: polling_delay = 2
  polling_delay = polling_delay - 1.5
  g_last_pigs = datetime.now()
  logging.info("Script started")
  while True:
    if datetime.now().day != g_last_days:
      g_last_days = datetime.now().day
      for x in range(24): 
        g_json_pvw[str(x)] = 0
        g_json_outw[str(x)] = 0
      g_json_data["day"] = g_last_days

    if datetime.now().minute != g_last_mins:
      g_last_mins = datetime.now().minute
      sum_pvw = sum_outw = 0
      tmp_pvw = {}
      tmp_outw = {}
      for x in range(24):
        sum_pvw = sum_pvw + g_json_pvw[str(x)]
        sum_outw = sum_outw + g_json_outw[str(x)]
        tmp_pvw[str(x)] = round(g_json_pvw[str(x)], 3)
        tmp_outw[str(x)] = round(g_json_outw[str(x)], 3)
      tmp_pvw["total"] = round(sum_pvw / 1000, 3)
      tmp_outw["total"] = round(sum_outw / 1000, 3)
      mqttclient.publish(mqtt_topic_pub + "/power/pvw", json.dumps(tmp_pvw))
      mqttclient.publish(mqtt_topic_pub + "/power/outw", json.dumps(tmp_outw))
      if 0 == g_last_mins % 10:
        g_json_data["pvw"] = g_json_pvw
        g_json_data["outw"] = g_json_outw
        with open(data_file, 'w') as f:
          json.dump(g_json_data, f)

    if g_mqttcmd != "":
      execute_command(g_mqttcmd)
      g_mqttcmd = ""
      g_cmdcnt = 99
    elif g_cmdcnt == -1:
      execute_command("QMOD")
    elif g_cmdcnt == 0:
      execute_command("QPIRI")
    else:
      execute_command("QPIGS")

    g_cmdcnt = g_cmdcnt + 1
    if g_cmdcnt >= 60: g_cmdcnt = 0

    time.sleep(polling_delay)
