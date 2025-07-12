from pandas.core import resample
import telnyx, os

telnyx.api_key = os.environ['TELNYX_API_KEY']

def sendSMS(to_phone: str, from_phone: str, message: str):
  
  response = telnyx.Message.create(from_= from_phone,to= to_phone, text= message)

  return response