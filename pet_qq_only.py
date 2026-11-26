import json, os, shutil

p = os.path.join(os.environ["APPDATA"], "dsh-pet-standalone-webm-chat", "config.json")
shutil.copyfile(p, p + ".bak-qqonly")

with open(p, "r", encoding="utf-8") as f:
    cfg = json.load(f)

enabled = cfg.get("feature_activation", {}).get("enabled", [])
before = list(enabled)
cfg["feature_activation"]["enabled"] = [x for x in enabled if x not in ("chat", "vision")]

with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)

print("before:", before)
print("after :", cfg["feature_activation"]["enabled"])
