from flask import Flask
from services.gptCompletion.routes import bp as gpt_bp
from services.sendSMS.routes import bp as sms_bp
from services.calcFormula.routes import bp as calc_bp

app = Flask(__name__)
app.register_blueprint(gpt_bp)
app.register_blueprint(sms_bp)
app.register_blueprint(calc_bp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)