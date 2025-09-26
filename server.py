# server.py
from flask import Flask, jsonify, Response
from flask_cors import CORS
import time, cv2
from back_end.scanner import scan_request, start_background_scanner, get_latest_frame, auth_status

app = Flask(__name__)
CORS(app)

# Start the background scanner loop
start_background_scanner()

# ------------------- ROUTES ------------------- #
@app.route("/start_scan", methods=["POST"])
def start_scan():
    if not scan_request["running"]:
        scan_request["running"] = True
        scan_request["start_time"] = time.time()
        return jsonify({"status": "scanning started"})
    else:
        return jsonify({"status": "already running"}), 400

@app.route("/stop_scan", methods=["POST"])
def stop_scan():
    scan_request["running"] = False
    return jsonify({"status": "scanning stopped"})

@app.route("/status", methods=["GET"])
def status():
    return jsonify(auth_status)

# ------------------- STREAMING ------------------- #
def gen_frames():
    while True:
        frame = get_latest_frame()
        if frame is not None:
            ret, buffer = cv2.imencode(".jpg", frame)
            if ret:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n")
        else:
            time.sleep(0.05)

@app.route("/video_feed")
def video_feed():
    return Response(gen_frames(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")

# ------------------- ENTRY ------------------- #
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
