HVAC update — Eastern Time and shared Recent Plots Y axes

1. Stop the receiver, Slack app and Recent Plots publisher windows.
2. Copy all five .py files from this package into reciever, replacing matching files.
   recent_plot_settings.py is a NEW required module. Keep data/ and existing launchers unchanged.
3. Run once in PowerShell: python -m pip install tzdata
4. Restart using your existing combined launcher. No channel ID or new Slack scopes are needed.
5. In Slack run /hvac again, then choose Recent Plots Y axes.
   Existing settings editors can save ranges. For each axis enter BOTH min and max,
   or leave BOTH blank for automatic limits calculated from the full last 30 days.
   Indoor/outdoor temperature and humidity each have independent bounds shared by all four images.
   Manual bounds may clip readings. They do not change alarm thresholds.
6. The running publisher applies changes on its next refresh (about 5 minutes).
   Settings persist in data/hvac.db. Existing alerts already posted are not rewritten.

Alerts and Canvas update time/time axes use America/New_York with automatic EST/EDT.
Storage stays UTC. Temperature/humidity panels touch at a common border.
Legend is on the right at size 10. No plot titles or alarm threshold lines were added.

Validated locally: four synthetic 12-sensor images, DST conversion, settings persistence,
permission denial, invalid bounds, stale-form conflict and Slack modal callbacks.
Live Slack delivery must be checked after installation.
