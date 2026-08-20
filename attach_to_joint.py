from robodk.robolink import *
from robodk.robomath import *
import math


def ask_joint_number(default_value=5):
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        joint = simpledialog.askinteger(
            "Select Joint",
            "Enter joint number to attach object to (e.g. 5):",
            initialvalue=default_value,
            minvalue=1,
            maxvalue=20
        )
        root.destroy()
        if joint is None:
            raise Exception("Canceled.")
        return int(joint)
    except Exception:
        while True:
            txt = input("Enter joint number (e.g. 5): ").strip()
            if txt.isdigit() and int(txt) >= 1:
                return int(txt)
            print("Enter a valid positive integer.")


def ask_pose_offset():
    """Pop up a single dialog with 6 fields: X Y Z Rx Ry Rz.
    Returns (dx, dy, dz, rx, ry, rz) in mm and degrees, or all zeros on cancel."""
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox

        root = tk.Tk()
        root.title("Offset relative to joint frame (optional)")
        root.resizable(False, False)

        fields = [
            ("X offset (mm)",  "0.0"),
            ("Y offset (mm)",  "0.0"),
            ("Z offset (mm)",  "0.0"),
            ("Rx offset (deg)", "0.0"),
            ("Ry offset (deg)", "0.0"),
            ("Rz offset (deg)", "0.0"),
        ]

        entries = []
        for row, (label, default) in enumerate(fields):
            tk.Label(root, text=label, anchor="w", width=18).grid(row=row, column=0, padx=10, pady=4, sticky="w")
            e = tk.Entry(root, width=12)
            e.insert(0, default)
            e.grid(row=row, column=1, padx=10, pady=4)
            entries.append(e)

        result = [None]

        def on_ok():
            values = []
            for e in entries:
                try:
                    values.append(float(e.get()))
                except ValueError:
                    messagebox.showerror("Invalid input", f"'{e.get()}' is not a valid number.")
                    return
            result[0] = values
            root.destroy()

        def on_skip():
            result[0] = [0.0] * 6
            root.destroy()

        btn_frame = tk.Frame(root)
        btn_frame.grid(row=len(fields), column=0, columnspan=2, pady=10)
        tk.Button(btn_frame, text="Apply Offset", command=on_ok, width=14).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Skip (no offset)", command=on_skip, width=14).pack(side="left", padx=6)

        root.mainloop()

        if result[0] is None:
            print("Offset dialog closed without selection — using zero offset.")
            return (0.0,) * 6
        return tuple(result[0])

    except Exception as ex:
        print(f"Could not open offset dialog ({ex}), using zero offset.")
        return (0.0,) * 6


# ── Main ──────────────────────────────────────────────────────────────────────

RDK = Robolink()

# 1) Pick robot
robot = RDK.ItemUserPick("Select the robot", ITEM_TYPE_ROBOT)
if not robot.Valid():
    raise Exception("No robot selected.")

# 2) Pick object
obj = RDK.ItemUserPick("Select the object to attach", ITEM_TYPE_OBJECT)
if not obj.Valid():
    raise Exception("No object selected.")

# 3) Pick joint number
joint_number = ask_joint_number(default_value=5)

# Try exact index first, then index-1 for models that use 0-based or offset numbering
for idx in [joint_number, joint_number - 1]:
    if idx < 0:
        continue
    link = robot.ObjectLink(idx)
    if link.Valid():
        target_link = link
        used_index = idx
        break
else:
    raise Exception(
        f"No valid link frame found for joint {joint_number}. "
        f"Tried indices {[joint_number, joint_number - 1]}."
    )

# 4) Reparent while preserving current world position
obj.setParentStatic(target_link)
print(f"Attached '{obj.Name()}' to '{robot.Name()}' link index {used_index} (joint {joint_number}).")

# 5) Optional pose offset relative to the joint frame
dx, dy, dz, rx_deg, ry_deg, rz_deg = ask_pose_offset()

if any(v != 0.0 for v in (dx, dy, dz, rx_deg, ry_deg, rz_deg)):
    offset = (
        transl(dx, dy, dz)
        * rotx(math.radians(rx_deg))
        * roty(math.radians(ry_deg))
        * rotz(math.radians(rz_deg))
    )
    obj.setPose(obj.Pose() * offset)
    print(f"Applied offset: XYZ=({dx}, {dy}, {dz}) mm  RxRyRz=({rx_deg}, {ry_deg}, {rz_deg}) deg")
else:
    print("No offset applied.")

print("Done.")
