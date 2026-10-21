const { execSync } = require("child_process");

function pythonPath() {
  try {
    const found = execSync("where python", { encoding: "utf8" })
      .split(/\r?\n/)
      .map((line) => line.trim())
      .find((line) => line && line.toLowerCase().endsWith("python.exe"));
    return found || "python";
  } catch {
    return "python";
  }
}

module.exports = {
  apps: [
    {
      name: "job-discovery-ksa",
      script: pythonPath(),
      args: "-m uvicorn api:app --host 127.0.0.1 --port 9002",
      cwd: __dirname,
      interpreter: "none",
      instances: 1,
      autorestart: true,
      watch: false,
      max_restarts: 100,
      min_uptime: 5000,
      restart_delay: 2000,
      exp_backoff_restart_delay: 2000,
      max_memory_restart: "1500M",
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
