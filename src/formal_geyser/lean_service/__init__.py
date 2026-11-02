"""The project's Lean service: one long-running process that starts every Lean and Lake process.

TaskHub, the command line and project setup are its clients. It schedules the machine's
cores and memory between the Integrator's batch validations (exclusive), the agents'
development checks (interactive lane) and official validations of frozen sources (job
lane), runs each Lean process in its job's cgroup and the Codex sandbox, and measures them. It runs
independently of TaskHub: a TaskHub restart or deployment leaves it, its jobs and its warm
library pages in place.
"""
