from axon.v2.configedit import ConfigEditor
from axon.v2.shadow import preview_impact


def test_shadow_preview_leaves_original_untouched_and_reports_edges(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("""from fastapi import FastAPI
app = FastAPI()
@app.get('/hello')
def hello():
    return 'hello'
""")
    path = tmp_path / "axon.yaml"
    original = "repo: example\nservices: [{id: app, roots: [src]}]\n"
    path.write_text(original)
    editor = ConfigEditor(tmp_path)
    plan = editor.add(
        "call:app/missed",
        {
            "from_api": "GET /hello",
            "to": {"service": "outside", "method": "GET", "path": "/x"},
            "evidence": {"file": "src/app.py", "line": 5, "text": "return 'hello'"},
        },
    )
    result = preview_impact(tmp_path, plan)
    assert result["graph_delta"]["outbound_added"]
    assert result["unresolved"]
    assert path.read_text() == original
    assert not list(tmp_path.rglob(".axon-*"))
