"""Report figures: equity curves (IS/OOS), slippage sensitivity, Monte Carlo distributions.
Palette: dataviz reference (validated): series-1 #2a78d6, series-2 #1baf7a, series-3 #eda100.
Direct labels used (relief rule for low-contrast slots). One axis per chart, recessive grid.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e5e4e0"
S1, S2, S3 = "#2a78d6", "#1baf7a", "#eda100"
BASE = 10_000.0

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "text.color": INK, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
    "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 11,
})


def style(ax):
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


# ---------------- fig 1: equity curves ----------------
fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
for ax, (title, path) in zip(axes, [
        ("IS 2026 (Jan–Jun) — total +4 849 USDT", "results/chosen_trades_day.csv"),
        ("OOS 2025 (full year) — total +2 808 USDT", "results/oos_trades_day.csv")]):
    tr = pd.read_csv(path).sort_values("exit_ms")
    t = pd.to_datetime(tr["exit_ms"], unit="ms")
    eq = BASE + tr["net"].cumsum()
    ax.plot(t, eq, color=S1, linewidth=2)
    ax.axhline(BASE, color=INK2, linewidth=0.8, linestyle="--", alpha=0.6)
    ax.annotate("base 10 000", xy=(t.iloc[0], BASE), fontsize=9, color=INK2,
                xytext=(0, 4), textcoords="offset points")
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    ax.set_ylabel("Equity, USDT (realized)")
    style(ax)
fig.suptitle("Day strategy — realized equity, base 10 000 USDT, flat 1 000/trade",
             fontsize=12, x=0.01, ha="left", fontweight="bold")
fig.tight_layout(rect=[0, 0, 1, 0.94])
fig.savefig("results/fig_equity.png", dpi=150)
plt.close(fig)

# ---------------- fig 2: slippage sensitivity ----------------
df = pd.read_csv("results/slippage_stress.csv")
oos = df[df.period == "OOS_2025"]
fig, ax = plt.subplots(figsize=(8, 4.6))
levels = [(0.05, S1), (0.15, S2), (0.30, S3)]
for lv, color in levels:
    sl = oos[(oos.s_entry == lv) & (oos.s_exit == lv)].sort_values("s_stop")
    ax.plot(sl.s_stop, sl.ci_low, color=color, linewidth=2, marker="o", markersize=5)
    ax.annotate(f"entry/exit {lv}%", xy=(sl.s_stop.iloc[-1], sl.ci_low.iloc[-1]),
                xytext=(6, 0), textcoords="offset points", color=INK, fontsize=10, va="center")
ax.axhline(0, color=INK2, linewidth=1.2, linestyle="--")
ax.annotate("edge unprovable below this line", xy=(0.05, 0), xytext=(0, -14),
            textcoords="offset points", color=INK2, fontsize=9)
ax.set_xlabel("Stop slippage s_stop, %")
ax.set_ylabel("Bootstrap CI lower bound of expectancy, USDT/trade")
ax.set_title("OOS 2025: edge survival vs slippage (frozen day config)", loc="left", color=INK)
ax.set_xlim(0, 1.15)
style(ax)
fig.tight_layout()
fig.savefig("results/fig_slippage.png", dpi=150)
plt.close(fig)

# ---------------- fig 3: Monte Carlo ----------------
ann = np.load("results/mc_oos_totals.npy") / BASE * 100  # OOS is exactly 365d
mdd = np.load("results/mc_oos_mdd.npy")
fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
for ax, data, label, unit in [(axes[0], ann, "Annual return", "%"),
                              (axes[1], mdd, "Max drawdown", "%")]:
    ax.hist(data, bins=60, color=S1, edgecolor=SURFACE, linewidth=0.4)
    for q, name in [(5, "p5"), (50, "p50"), (95, "p95")]:
        v = np.percentile(data, q)
        ax.axvline(v, color=INK2, linewidth=1, linestyle="--")
        ax.annotate(f"{name} {v:.1f}{unit}", xy=(v, ax.get_ylim()[1]), xytext=(2, -12),
                    textcoords="offset points", fontsize=9, color=INK2)
    ax.set_title(f"{label} — 10 000 bootstrap paths (OOS 2025 trades)", loc="left", fontsize=11, color=INK)
    ax.set_xlabel(f"{label}, {unit}")
    ax.set_ylabel("Paths")
    style(ax)
fig.tight_layout()
fig.savefig("results/fig_montecarlo.png", dpi=150)
plt.close(fig)

print("saved results/fig_equity.png, results/fig_slippage.png, results/fig_montecarlo.png")
