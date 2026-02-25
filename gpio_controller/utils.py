# -*- coding: utf-8 -*-
"""
센서 데이터 계산 등 유틸 (개발자 구현)
- process_sensor_data: 가스 측정 row를 DB 포맷 한 레코드로 정리 (gas_id, test_id는 main에서 설정).
"""
from schema import build_empty_measurement
try:
    import RPi.GPIO as GPIO
except (ImportError, ModuleNotFoundError):
    GPIO = None  # Mac/PC 등 비라즈베리파이 환경 또는 GPIO_SIMULATION 시
import os,time,gc,math,requests,json,shutil
from collections import OrderedDict


def process_sensor_data(raw_gas, raw_camera):
    """
    가스/카메라 원시 데이터를 DB 스키마 포맷 한 레코드로 가공.
    - gas_id, test_id 는 main.py에서 넣음.
    - 현재 DB 스키마는 가스 필드만 포함; 카메라는 추후 확장 시 raw_camera 반영.
    :param raw_gas: gas_controller.measure_once() 결과
    :param raw_camera: camera_controller.capture_once() 결과 (미사용 시 무시)
    :return: dict - gas 측정 필드만 포함 (gas_id, test_id 제외)
    """
    out = build_empty_measurement()
    # gas_id, test_id 제외한 키만 raw_gas에서 복사
    for key in out:
        if key in ("profile_id", "gas_id", "test_id"):
            continue
        if raw_gas and key in raw_gas:
            out[key] = raw_gas[key]
    return out

def send_image_to_serve(image_path,server_url):
    ans = 0
    try:
        with open(image_path,'rb') as file:
            files = {'file':file}
            response = requests.post(server_url,files=files)
            ans = response.status_code
            if ans == 200:
                print('image sending success',response.text)
            else:
                print('image sending fail: ',response.status_code)
    except Exception as e:
        print('Image Error',e)
    return ans
        
def configure_wifi(ssid,password):
    idx = 0
    while True:
        try:
            config_lines = [
                'ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev',
                'update_config=1',
                'country=US',
                '\n',
                'network={',
                '\tssid="{}"'.format(ssid),
                '\tpsk="{}"'.format(password),
                '}'
                ]
            config = '\n'.join(config_lines)

            os.popen("sudo chmod a+w /etc/wpa_supplicant/wpa_supplicant.conf")
            with open("/etc/wpa_supplicant/wpa_supplicant.conf","w") as wifi:
                wifi.write(config)
            os.popen("sudo wpa_cli -i wlan0 reconfigure")
            yellow_led_val = 1
            break
        except PermissionError:
            print('!')
            yellow_led_val = 0
            pass
    return yellow_led_val
    
def FileGeneration_test_id(var):
    with open('/home/pi/ABElectronics_Python3_Libraries/ADCPi/Test_num.txt','r') as f:
        lines = f.readlines()
    try:
        lines_num = int(lines[-1])+1
        error_is = 'N'
    except:
        error_is = 'Y'
    del f
    gc.collect()
    print(error_is)
    
    if error_is == 'Y':
        lines_num = int(lines[-2])+1
        Num_num = lines_num%100000
        #FileName = var+str(Num_num).zfill(5)
        FileName = str(Num_num).zfill(5)
        with open('/home/pi/ABElectronics_Python3_Libraries/ADCPi/Test_num.txt','a') as f:
            f.write('\n'+str(lines_num)+'\n')
        del f
        gc.collect()
    elif error_is == 'N':
        Num_num = lines_num%100000
        #FileName = var+str(Num_num).zfill(5)
        FileName = str(Num_num).zfill(5)
        with open('/home/pi/ABElectronics_Python3_Libraries/ADCPi/Test_num.txt','a') as f:
            f.write(str(lines_num)+'\n')
        del f
        gc.collect()
    
    return FileName

def filter(c, b, a):
    if abs(c-b) > 0.01:
        if a == 0:
            x = b
        else :
            if abs(b-a) > 0.01:
                if (a-b)*(b-c) < 0 :
                    if abs(a-c) > 0.009:
                        x = a
                    else :
                        x = (a+c)/2
                
                elif (a-b)*(b-c) > 0 :
                    x = a

            else :
                x = b

    else : 
        x = b

    return x, c, b

def LEDs(COLOR):
    if GPIO is None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(17,GPIO.OUT)
    GPIO.setup(27,GPIO.OUT)
    GPIO.setup(22,GPIO.OUT)
    
    if COLOR == 'RED':#RED
        GPIO.output(17,1)
        GPIO.output(27,1)
        GPIO.output(22,0)
    elif COLOR == 'GREEN':#GREEN
        GPIO.output(17,1)
        GPIO.output(27,0)
        GPIO.output(22,1)
    elif COLOR == 'BLUE':#BLUE
        GPIO.output(17,0)
        GPIO.output(27,1)
        GPIO.output(22,1)
    elif COLOR == 'NO':
        GPIO.cleanup(17)
        GPIO.cleanup(27)
        GPIO.cleanup(22)


def Camera_LED(ONOFF):
    if GPIO is None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(23,GPIO.OUT)
    
    if ONOFF == 'ON':
        GPIO.output(23,True)
    elif ONOFF == 'OFF':
        GPIO.output(23,False)

def WIFI_LED(ONOFF):
    if GPIO is None:
        return
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(24,GPIO.OUT) #3
    
    if ONOFF == 'ON':
        GPIO.output(24,True)
    elif ONOFF == 'OFF':
        GPIO.output(24,False)

def mean(data):
    ans = 0
    ans = sum(data)/len(data)
    
    return ans

def stdev(data,xbar):
    ans,total = 0,0
    for x in data:
        total += (x-xbar)**2
    if len(data)!=0:
        ans = math.sqrt(total/len(data))
    return ans