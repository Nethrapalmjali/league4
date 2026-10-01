import os
import sys

# Add root directory to path so app.py can be imported
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, root_dir)

import aws_lambda_wsgi
from app import app

def handler(event, context):
    return aws_lambda_wsgi.response(app, event, context)
