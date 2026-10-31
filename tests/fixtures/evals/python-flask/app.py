from flask import Flask
app = Flask(__name__)
@app.route('/items', methods=['GET', 'POST'])
def items(): pass
