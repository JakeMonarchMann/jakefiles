import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

# --- Load data ---
df = pd.read_csv(
    r"C:\Users\jake.mann\monarchquantum\Active Optics Alignment Team - General\03-Engineering\00-Active Process Development\00-Activity\APD-0008 TEC Control\TECDataCollection.csv",
    skiprows=2,  # skip the two header lines ("Test started..." and "Query used...")
)

# Convert time from ms to seconds
df["Time(s)"] = df["Time(ms)"] / 1000.0

# --- Plot setup ---
fig = plt.figure(figsize=(14, 10))
fig.suptitle("TECSource Data Log", fontsize=16, fontweight="bold", y=0.98)
gs = gridspec.GridSpec(3, 2, figure=fig, hspace=0.5, wspace=0.35)

ax1 = fig.add_subplot(gs[0, :])   # Temperature spans full width (most important)
ax2 = fig.add_subplot(gs[1, 0])   # Current
ax3 = fig.add_subplot(gs[1, 1])   # Voltage
ax4 = fig.add_subplot(gs[2, 0])   # Resistance
ax5 = fig.add_subplot(gs[2, 1])   # Output (on/off)

t = df["Time(s)"]

# --- Temperature ---
ax1.plot(t, df["TEC Sensor T"], color="#e05a2b", linewidth=1.8, label="Temperature (°C)")
ax1.set_ylabel("Temperature (°C)")
ax1.set_xlabel("Time (s)")
ax1.set_title("Sensor Temperature")
ax1.grid(True, alpha=0.3)
ax1.axhline(30, color="gray", linestyle="--", linewidth=1, alpha=0.6, label="30°C setpoint")
ax1.legend(fontsize=9)

# --- Current ---
ax2.plot(t, df["TEC Current"], color="#2b7be0", linewidth=1.4)
ax2.set_ylabel("Current (A)")
ax2.set_xlabel("Time (s)")
ax2.set_title("TEC Current")
ax2.grid(True, alpha=0.3)

# --- Voltage ---
ax3.plot(t, df["TEC Voltage"], color="#9b2be0", linewidth=1.4)
ax3.set_ylabel("Voltage (V)")
ax3.set_xlabel("Time (s)")
ax3.set_title("TEC Voltage")
ax3.grid(True, alpha=0.3)

# --- Thermistor Resistance ---
ax4.plot(t, df["TEC Sensor R"], color="#2ba85e", linewidth=1.4)
ax4.set_ylabel("Resistance (kΩ)")
ax4.set_xlabel("Time (s)")
ax4.set_title("Thermistor Resistance")
ax4.grid(True, alpha=0.3)

# --- TEC Output (on/off) ---
ax5.step(t, df["TEC Output"], color="#e0a02b", linewidth=1.4, where="post")
ax5.set_ylabel("Output State")
ax5.set_xlabel("Time (s)")
ax5.set_title("TEC Output (On/Off)")
ax5.set_yticks([0, 1])
ax5.set_yticklabels(["Off", "On"])
ax5.grid(True, alpha=0.3)

plt.savefig("tec_plot.png", dpi=150, bbox_inches="tight")
print("Saved tec_plot.png")
plt.show()