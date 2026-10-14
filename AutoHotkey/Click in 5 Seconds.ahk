#Requires AutoHotkey v2.0
#SingleInstance Force

clickDeadline := 0

^+5:: {
	global clickDeadline
	SetTimer(ClickCountdown, 0)
	ToolTip()
	response := InputBox("How many seconds should I wait?", "Click timer", "w260 h120", "5")
	if response.Result != "OK"
		return
	if !IsNumber(response.Value) || response.Value <= 0 {
		MsgBox("Enter a number greater than 0.", "Invalid wait", "Icon!")
		return
	}
	clickDeadline := A_TickCount + Round(response.Value * 1000)
	SetTimer(ClickCountdown, 100)
	ClickCountdown()
}

ClickCountdown() {
	global clickDeadline
	remainingMs := clickDeadline - A_TickCount
	if remainingMs <= 0 {
		SetTimer(ClickCountdown, 0)
		ToolTip()
		Click
		return
	}
	remaining := Ceil(remainingMs / 1000)
	ToolTip("Clicking in " . remaining . " second" . (remaining = 1 ? "" : "s") . ".")
}

