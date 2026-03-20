import flask
from flask import request, jsonify
import requests

app = flask.Flask(__name__)

@app.route('/analyze', methods=['POST'])
def analyze_repo():
    data = request.get_json()
    repo_url = data['repo_url']
    parts = repo_url.rstrip('/').split('/')
    owner, name = parts[-2], parts[-1]
    
    resp = requests.get(
        f"https://api.github.com/repos/{owner}/{name}/issues",
        params={"state": "open", "per_page": 10},
        timeout=15
    ).json()
    
    if not isinstance(resp, list):
        return jsonify({"error": "GitHub API error", "detail": resp}), 500
    
    summary = {
        "repo": f"{owner}/{name}",
        "top_issues": [{"title": i.get("title"), "labels": [l["name"] for l in i.get("labels", [])]} for i in resp[:5]]
    }
    return jsonify(summary)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)
