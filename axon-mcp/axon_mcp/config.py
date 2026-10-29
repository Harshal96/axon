import json
import os
from pathlib import Path
from pydantic import BaseModel

class Config(BaseModel):
    api_url: str = "http://localhost:8080"
    token: str = ""

def get_config_path() -> Path:
    return Path(os.path.expanduser("~/.axon/config.json"))

def load_config() -> Config:
    path = get_config_path()
    if path.exists():
        with open(path, "r") as f:
            return Config(**json.load(f))
    return Config()

def save_config(config: Config) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(config.model_dump_json(indent=2))

def configure_claude(api_url: str, token: str):
    # Save the local axon config
    cfg = Config(api_url=api_url, token=token)
    save_config(cfg)
    
    # Update claude code config
    claude_path = Path(os.path.expanduser("~/.claude.json"))
    
    config = {}
    if claude_path.exists():
        with open(claude_path, "r") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                pass
            
    mcp_servers = config.get("mcpServers", {})
    project_dir = Path(__file__).resolve().parent.parent
    mcp_servers["axon"] = {
        "command": "uv",
        "args": ["run", "--project", str(project_dir), "axon-mcp", "serve"]
    }
    config["mcpServers"] = mcp_servers
    
    with open(claude_path, "w") as f:
        json.dump(config, f, indent=2)
