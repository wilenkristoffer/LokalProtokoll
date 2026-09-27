"""Measure what a run uses: RAM and CPU of the process and its children (psutil),
and GPU memory and load (Windows performance counters, the numbers Task Manager
shows; they work for AMD, Intel and NVIDIA).

GPU memory is measured for the whole graphics card, so the monitor records a
baseline before the run and reports the increase. Close other GPU-heavy
programs while benchmarking.
"""

import json
import subprocess
import threading
import time

import psutil

# One long-running PowerShell loop prints "vram_mb gpu_percent" about once a second.
# GPU load = the busiest engine type (3D or Compute; Vulkan work shows up there).
GPU_SAMPLER = r"""
$paths = '\GPU Adapter Memory(*)\Dedicated Usage', '\GPU Engine(*)\Utilization Percentage'
while ($true) {
    $s = (Get-Counter $paths -ErrorAction SilentlyContinue).CounterSamples
    $mem = ($s | Where-Object { $_.Path -like '*adapter memory*' } | Measure-Object CookedValue -Maximum).Maximum
    $load = @{}
    foreach ($x in ($s | Where-Object { $_.Path -like '*gpu engine*' })) {
        if ($x.InstanceName -match 'engtype_(3d|compute)') {
            $k = $x.InstanceName -replace '^.*(luid_[^_]+_[^_]+).*engtype_(\w+).*$', '$1 $2'
            $load[$k] = $load[$k] + $x.CookedValue
        }
    }
    $top = ($load.Values | Measure-Object -Maximum).Maximum
    if ($top -eq $null) { $top = 0 }
    [Console]::Out.WriteLine(("{0} {1}" -f [math]::Round($mem / 1MB), [math]::Round([math]::Min($top, 100))))
    [Console]::Out.Flush()
}
"""


class ResourceMonitor:
    def __init__(self, interval=0.5):
        self.interval = interval
        self.samples = []       # {"t", "ram_mb", "cpu_pct"}
        self.gpu_samples = []   # {"t", "vram_mb", "gpu_pct"}
        self.baseline_vram = None
        self._stop = threading.Event()
        self._gpu_proc = None

    def start(self, timeout=15):
        """Start sampling the GPU and record the idle GPU memory. Call this before
        starting the work, then attach() the work's process."""
        self._gpu_proc = subprocess.Popen(["powershell", "-NoProfile", "-Command", GPU_SAMPLER],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read_gpu, daemon=True).start()
        end = time.time() + timeout
        while len(self.gpu_samples) < 2 and time.time() < end:
            time.sleep(0.1)
        if self.gpu_samples:
            self.baseline_vram = min(s["vram_mb"] for s in self.gpu_samples)

    def attach(self, pid):
        """Start sampling RAM and CPU of this process and its children."""
        threading.Thread(target=self._sample_process, args=(pid,), daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._gpu_proc:
            self._gpu_proc.kill()

    def _read_gpu(self):
        for line in self._gpu_proc.stdout:
            parts = line.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                self.gpu_samples.append({"t": time.time(), "vram_mb": int(parts[0]), "gpu_pct": int(parts[1])})

    def _sample_process(self, pid):
        procs = {}
        cpus = psutil.cpu_count() or 1
        while not self._stop.is_set():
            try:
                root = psutil.Process(pid)
                tree = [root] + root.children(recursive=True)
            except psutil.NoSuchProcess:
                tree = []
            ram, cpu = 0, 0.0
            for p in tree:
                try:
                    # cpu_percent needs the same Process object across calls.
                    proc = procs.setdefault(p.pid, p)
                    cpu += proc.cpu_percent(None)
                    ram += proc.memory_info().rss
                except psutil.Error:
                    pass
            self.samples.append({"t": time.time(), "ram_mb": round(ram / 2**20), "cpu_pct": round(cpu / cpus, 1)})
            time.sleep(self.interval)

    def summary(self, start, end):
        """Peak RAM, average CPU, peak extra VRAM and average GPU load between two times."""
        ps = [s for s in self.samples if start <= s["t"] <= end]
        gs = [s for s in self.gpu_samples if start <= s["t"] <= end]
        out = {"peak_ram_mb": max((s["ram_mb"] for s in ps), default=None),
               "avg_cpu_pct": round(sum(s["cpu_pct"] for s in ps) / len(ps), 1) if ps else None,
               "peak_vram_mb": None, "avg_gpu_pct": None}
        if gs:
            base = self.baseline_vram or 0
            out["peak_vram_mb"] = max(0, max(s["vram_mb"] for s in gs) - base)
            out["avg_gpu_pct"] = round(sum(s["gpu_pct"] for s in gs) / len(gs), 1)
        return out


def hardware_info():
    """CPU, RAM, GPU and OS, for the report."""
    script = r"""
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $os = Get-CimInstance Win32_OperatingSystem
    $gpus = @()
    Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}' -ErrorAction SilentlyContinue |
      ForEach-Object {
        $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($p.DriverDesc -and $p.'HardwareInformation.qwMemorySize') {
          $gpus += @{ name = $p.DriverDesc; vram_gb = [math]::Round($p.'HardwareInformation.qwMemorySize' / 1GB); driver = $p.DriverVersion }
        }
      }
    @{ cpu = $cpu.Name.Trim(); cores = $cpu.NumberOfCores; threads = $cpu.NumberOfLogicalProcessors;
       ram_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB); os = $os.Caption; gpus = $gpus } | ConvertTo-Json -Depth 3
    """
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                                timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        info = json.loads(result.stdout)
        if isinstance(info.get("gpus"), dict):
            info["gpus"] = [info["gpus"]]
        return info
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}
