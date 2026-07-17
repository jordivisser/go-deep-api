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
from go_deep_ranker import go_deep, write_excel, to_json, apollo_enrich, extract_rfq_metadata

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
      - rfq_file: PDF (optional)
      - project_name: string (required)
      - sector: string (default: heavy_civil)

    Returns JSON with tier summary, ranked orgs, Apollo contacts,
    RFQ metadata, and base64-encoded Excel.
    """
    if "attendee_file" not in request.files:
        return jsonify({"error": "attendee_file is required"}), 400

    file = request.files["attendee_file"]
    rfq_file = request.files.get("rfq_file")
    project_name = request.form.get("project_name", "Untitled Project")
    sector = request.form.get("sector", "heavy_civil")

    if not file.filename:
        return jsonify({"error": "Empty file"}), 400

    # Save uploaded files
    run_id = uuid.uuid4().hex[:8]
    ext = os.path.splitext(file.filename)[1] or ".pdf"
    input_path = os.path.join(UPLOAD_DIR, f"{run_id}{ext}")
    file.save(input_path)

    rfq_path = None
    if rfq_file and rfq_file.filename:
        rfq_ext = os.path.splitext(rfq_file.filename)[1] or ".pdf"
        rfq_path = os.path.join(UPLOAD_DIR, f"{run_id}_rfq{rfq_ext}")
        rfq_file.save(rfq_path)

    try:
        # Run the ranker
        attendees = go_deep(input_path, sector=sector)

        # Apollo enrichment — pull contacts for Tier 1-3 orgs
        tier_1_3_orgs = set()
        for a in attendees:
            if a.tier <= 3:
                tier_1_3_orgs.add(a.canonical_org or a.organization)
        apollo_contacts = apollo_enrich(sorted(tier_1_3_orgs)) if tier_1_3_orgs else {}

        # RFQ extraction (if uploaded)
        rfq_metadata = None
        if rfq_path:
            rfq_metadata = extract_rfq_metadata(rfq_path)

        # Generate Excel
        excel_filename = f"Go_Deep_{run_id}.xlsx"
        excel_path = os.path.join(OUTPUT_DIR, excel_filename)
        write_excel(attendees, excel_path)

        # Read Excel as base64 for inline download
        with open(excel_path, "rb") as f:
            excel_b64 = base64.b64encode(f.read()).decode("utf-8")

        # Build JSON response
        result = to_json(attendees, project_name=project_name, apollo_contacts=apollo_contacts)
        result["excelBase64"] = excel_b64
        result["excelFilename"] = f"Go_Deep_{project_name.replace(' ', '_')}.xlsx"
        result["runId"] = run_id
        if rfq_metadata:
            result["rfqMetadata"] = rfq_metadata

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Clean up input files
        if os.path.exists(input_path):
            os.remove(input_path)
        if rfq_path and os.path.exists(rfq_path):
            os.remove(rfq_path)


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