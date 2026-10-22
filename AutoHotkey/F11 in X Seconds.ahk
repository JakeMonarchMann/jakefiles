#Requires AutoHotkey v2.0
#SingleInstance Force

f11Deadline := 0

^+6:: {
	global f11Deadline
	SetTimer(F11Countdown, 0)
	ToolTip()
	response := InputBox("How many seconds should I wait?", "F11 timer", "w260 h120", "5")
	if response.Result != "OK"
		return
	if !IsNumber(response.Value) || response.Value <= 0 {
		MsgBox("Enter a number greater than 0.", "Invalid wait", "Icon!")
		return
	}
	f11Deadline := A_TickCount + Round(response.Value * 1000)
	SetTimer(F11Countdown, 100)
	F11Countdown()
}

F11Countdown() {
	global f11Deadline
	remainingMs := f11Deadline - A_TickCount
	if remainingMs <= 0 {
		SetTimer(F11Countdown, 0)
		ToolTip()
		Send("{F11}")
		return
	}
	remaining := Ceil(remainingMs / 1000)
	ToolTip("Pressing F11 in " . remaining . " second" . (remaining = 1 ? "" : "s") . ".")
}
