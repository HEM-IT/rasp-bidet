import board
import digitalio
from PIL import Image,ImageDraw,ImageFont
import adafruit_ssd1306

def SSD1306_DISPLAY(GAS_ID,FILENAME):
	#oled_reset = digitalio.DigitalInOut(board.D4)
	
	WIDTH = 128
	HEIGHT =64
	BORDER=5
	
	i2c = board.I2C()
	oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c)
	#oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c,reset = oled_reset)
	
	oled.fill(0)
	oled.show()
	
	image = Image.new('1',(oled.width,oled.height))
	
	draw = ImageDraw.Draw(image)
	draw.rectangle(
		(BORDER,BORDER,oled.width-BORDER-1,oled.height-BORDER-1),
		outline=0,
		fill=0,
	)
	
	#font = ImageFont.load_default()
	font_1 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',24)
	font_2 = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',26)
	
	text_1 = GAS_ID
	text_2 = FILENAME
	
	bbox_1 = font_1.getbbox(text_1)
	bbox_2 = font_2.getbbox(text_2)
	(font_width_1,font_height_1) = font_1.getsize(text_1)
	(font_width_2,font_height_2) = font_2.getsize(text_2)
	(font_width_1,font_height_1)=bbox_1[2]-bbox_1[0],bbox_1[3]-bbox_1[1]
	(font_width_2,font_height_2)=bbox_2[2]-bbox_2[0],bbox_2[3]-bbox_2[1]
	draw.text(
		(oled.width//2 - font_width_1//2,oled.height//2-
		font_height_1//2 - 15),
		text_1,
		font = font_1,
		fill = 255,
	)
	draw.text(
		(oled.width//2 - font_width_2//2,oled.height//2-
		font_height_2//2 + 10),
		text_2,
		font = font_2,
		fill = 255,
	)

	oled.image(image)
	oled.show()
	
"""
def SSD1306_DISPLAY(FILENAME):
	#oled_reset = digitalio.DigitalInOut(board.D4)

	WIDTH = 128
	HEIGHT =64
	BORDER=5

	i2c = board.I2C()
	oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c)
	#oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c,reset = oled_reset)

	oled.fill(0)
	oled.show()

	image = Image.new('1',(oled.width,oled.height))

	draw = ImageDraw.Draw(image)
	draw.rectangle(
		(BORDER,BORDER,oled.width-BORDER-1,oled.height-BORDER-1),
		outline=0,
		fill=0,
	)

	#font = ImageFont.load_default()
	font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)
	
	text = FILENAME
	bbox = font.getbbox(text)
	(font_width,font_height) = font.getsize(text)
	(font_width,font_height)=bbox[2]-bbox[0],bbox[3]-bbox[1]
	draw.text(
		(oled.width//2 - font_width//2,oled.height//2-
		font_height//2),
		text,
		font = font,
		fill = 255,
	)

	oled.image(image)
	oled.show()
"""
def Reset_Display():
	WIDTH = 128
	HEIGHT =64
	BORDER=5

	i2c = board.I2C()
	oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c)
	#oled = adafruit_ssd1306.SSD1306_I2C(WIDTH,HEIGHT,i2c,addr = 0x3c,reset = oled_reset)

	oled.fill(0)
	oled.show()

