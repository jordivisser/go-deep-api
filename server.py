"""
Go Deep API — Flask server wrapping the attendee ranker.
Receives PDF/Excel uploads, runs the ranker, returns JSON + Excel download.

Deploy on Dokploy alongside n8n.
"""

import os
import uuid
import base64
import tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from go_deep_ranker import go_deep, write_excel, to_json

app = Flask(__name__)
CORS(app)

UPLOAD_DIR = tempfile.mkdtemp(prefix="godeep_")
OUTPUT_DIR = tempfile.mkdtemp(prefix="godeep_out_")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/go-deep", methods=["POST"])
def run_go_deep():
    """
    Accepts multipart form:
      - attendee_file: PDF or Excel (required)
      - project_name: string (required)
      - sector: string (default: heavy_civil)

    Returns JSON with tier summary, ranked orgs, and base64-encoded Excel.
    """
    if "attendee_file" not in request.files:
        return jsonify({"error": "attendee_file is required"}), 400

    file = request.files["attendee_file"]
    project_name = request.form.get("project_name", "Untitled Project")
    sector = request.form.get("sector", "heavy_civil")

    if not file.filename:
        return jsonify({"error": "Empty file"}), 400

    # Save uploaded file
    run_id = uuid.uuid4().hex[:8]
    ext = os.path.splitext(file.filename)[1] or ".pdf"
    input_path = os.path.join(UPLOAD_DIR, f"{run_id}{ext}")
    file.save(input_path)

    try:
        # Run the ranker
        attendees = go_deep(input_path, sector=sector)

        # Generate Excel
        excel_filename = f"Go_Deep_{run_id}.xlsx"
        excel_path = os.path.join(OUTPUT_DIR, excel_filename)
        write_excel(attendees, excel_path)

        # Read Excel as base64 for inline download
        with open(excel_path, "rb") as f:
            excel_b64 = base64.b64encode(f.read()).decode("utf-8")

        # Build JSON response
        result = to_json(attendees, project_name=project_name)
        result["excelBase64"] = excel_b64
        result["excelFilename"] = f"Go_Deep_{project_name.replace(' ', '_')}.xlsx"
        result["runId"] = run_id

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Clean up input file
        if os.path.exists(input_path):
            os.remove(input_path)


@app.route("/api/go-deep/download/<run_id>", methods=["GET"])
def download_excel(run_id: str):
    """Download a previously generated Excel file by run ID."""
    for f in os.listdir(OUTPUT_DIR):
        if run_id in f:
            return send_file(
                os.path.join(OUTPUT_DIR, f),
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True,
                download_name=f,
            )
    return jsonify({"error": "File not found"}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)