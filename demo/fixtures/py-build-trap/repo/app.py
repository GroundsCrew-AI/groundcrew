import os

import psycopg2
from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/health")
def health():
    return jsonify(status="ok")


@app.route("/version")
def version():
    with psycopg2.connect(os.environ.get("DATABASE_URL")) as conn, conn.cursor() as cur:
        cur.execute("SELECT version()")
        return jsonify(version=cur.fetchone()[0])
