#!/bin/bash
# Query XPU status on a remote node via xpu-smi.
# Outputs: <ip>: <STATUS> | Mem: <avg_mib> MiB | Util: <avg_pct> % | Container: <name>

cmd=$(cat <<'EOF'
ip=$(hostname -I | awk '{print $1}');
output=$(xpu-smi);
if echo $output | grep -q "No running processes found"; then
    status="\033[0;32mFREE\033[0m";
else
    status="\033[0;31mBUSY\033[0m";
    pid=$(echo "$output" | grep -E 'N/A  N/A\s*[0-9]+' | head -n 1 | awk '{print $5}');
    cname="";
    if [ -e "/proc/$pid/cgroup" ]; then
        line=$(grep -E 'docker|containerd' /proc/$pid/cgroup 2>/dev/null | head -n1);
        cg=$(echo "$line" | sed -E 's#.*/docker[-/]?([0-9a-f]+).*#\1#' | cut -c1-7);
        if [ -n "$cg" ]; then
            cname=$(docker ps --format '{{.ID}} {{.Names}}' | grep "$cg" | awk '{print $2}');
        fi
    fi
fi;
output=$(xpu-smi -m);
mems=($(echo "$output" | awk '{print $18}'));
utils=($(echo "$output" | awk '{print $20}'));
mem_sum=0; util_sum=0; count=${#mems[@]};
for ((i=0; i<count; i++)); do
    mem_sum=$((mem_sum + mems[i]));
    util_sum=$((util_sum + utils[i]));
done;
avg_mem=$(echo "scale=2; $mem_sum / $count" | bc);
avg_util=$(echo "scale=2; $util_sum / $count" | bc);
echo -e "${ip}: $status | Mem: ${avg_mem} MiB | Util: ${avg_util} % | Container: ${cname:-}";
EOF
)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$SCRIPT_DIR/cmd.py" bash -c "$cmd"
