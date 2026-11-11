# Time Machine and APFS Snapshots

For snapshot questions, use `tmutil listlocalsnapshots <mount-point>` and `diskutil apfs listSnapshots <resolved-device-or-volume>`, resolving the target with `diskutil info` rather than assuming a disk identifier.

The supported command/API inventory reports snapshot identity and purgeability, not Disk Utility's per-snapshot `Private Size` or `Cumulative Size`. Keep both fields null with `size_status=MANUAL-ONLY` and a reason. Do not infer them from `du`, a mounted snapshot, logical presence or shared APFS capacity.

For manual measurement, open Disk Utility, choose View > Show APFS Snapshots, select the volume, then Show Columns > Private Size and Cumulative Size. Keep these values separate; neither is an additive reclaim total.

Snapshots are system-managed recovery assets, not routine junk. Consider deletion only as a separately approved advanced action under actual space pressure, using supported `tmutil` and without promising a retained size. Apply the [action policy](action-policy.md) and the parent skill's exact-approval and per-target verification gates.
