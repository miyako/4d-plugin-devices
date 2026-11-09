# 4d-plugin-devices

Devices lists the CD/DVD drives connected to the computer. It fills six `Text` arrays, one element per drive. **On macOS** it asks the DiscRecording framework (`DRDevice`) for the attached optical devices and returns each one's display name, connection type, vendor, product name, and whether it can write CDs and DVDs. **On Windows** it walks the drive letters and returns the ones whose storage driver reports a CD-ROM or DVD device type, with that type.

| Command | Returns | Purpose |
|---|---|---|
| [DEVICES LIST](#devices-list) | six `Text` arrays | List the CD/DVD drives and their properties |

**Platforms:** macOS (Apple silicon and Intel, macOS 11.5 or later), Windows (64-bit)

Releases: [github.com/miyako/4d-plugin-devices/releases](https://github.com/miyako/4d-plugin-devices/releases)

## Maintainer release lifecycle

Run **Bump version** manually on the desired branch, choosing patch, minor or
major. It validates and commits only `Devices.xcodeproj/project.pbxproj` and
`Info.plist`, then pushes without force or follow-tags. It rejects a branch that
has moved since dispatch (including during push). It creates no tag and does not
start Build.

Run **Build** manually on the updated branch when ready. There are no typed
inputs: the Actions branch selector selects the source snapshot. Every checkout
uses the immutable dispatch `github.sha`. Pushing a branch or tag does not start
Build. The strict `X.Y.Z` source version supplies `vX.Y.Z` and the existing
`Devices-X.Y.Z.zip` / `Devices-X.Y.Z.dmg` asset names.

The source authority is `MARKETING_VERSION`; all configurations'
`CURRENT_PROJECT_VERSION` and literal source plist version fields must agree.
The modernization commit established these coordinated fields, not independent
build counters. Recognized `$(MARKETING_VERSION)` / `${MARKETING_VERSION}` and
`$(CURRENT_PROJECT_VERSION)` / `${CURRENT_PROJECT_VERSION}` plist references are
preserved; `CURRENT_PROJECT_VERSION` may reference `MARKETING_VERSION`.
An absent `CFBundleVersion` stays absent. Prebuilt bundle plists, dependencies,
4D format versions and manifest IDs are not release-version sources.

Build retains Windows x64, macOS universal verification, cross-platform
packaging, Developer ID signing, notarization of both assets and DMG stapling.
Publication is serialized repository-wide across branches. Before building and
again after all these gates, authenticated, paginated release listing rejects
any same-tag release (including drafts), and a remote-ref check rejects any
existing tag. API/auth/network/schema errors fail closed, including on later
pages. Only a complete successful no-match listing establishes absence.

Publication creates a new, non-force exact-SHA tag and verifies it remotely;
even a same-SHA creation race is rejected. It then uses a create-only release
POST with explicit tag, SHA, name, `draft=false`, `prerelease=false` and an empty
body (the original notes policy: no generated notes). Assets are uploaded only
after release creation, without clobbering. It never updates or deletes existing
tags, releases or assets, and does not roll back or retry failed publication.

**Recovery:** a failed run may leave a tag, a public release with no assets, or a
partially uploaded asset set. Inspect the failed step, remote tag SHA, release
ID and uploaded assets before deciding on manual recovery. Such partial state
is intentionally retained, and rerunning Build for that version will reject it.
Do not blindly retry or automate deletion; reconcile the state explicitly, or
bump source to a new version for a new release. Listing failures require fixing
API access, not treating an error or 404 as permission to publish.

Local lifecycle checks (no native build, signing or remote publication):
`python3 -m unittest discover -s .github/tests -v` and
`actionlint -ignore SC2013 .github/workflows/*.yml` (ignores the existing
prebuilt-library loop's ShellCheck advisory, not workflow validation).

---

## Requirements & platform notes

- **Pass all six arrays on both platforms.** The manifest declares six array parameters (`DEVICES LIST(&Y;&Y;&Y;&Y;&Y;&Y)`), and the plugin writes to all six on both platforms. The original example called it with two arrays on Windows; don't rely on that form.
- **The arrays mean different things on each platform.** On macOS all six are filled. On Windows only the first two carry data (drive letter and media type); the other four get one empty string per drive, so every array has the same size.
- **No drive means empty arrays, not an error.** Most current Macs have no optical drive; the command then returns six arrays of size 0. It never raises a 4D error.
- **The writing capabilities are text, not booleans.** On macOS the fifth and sixth arrays hold `"1"` or `"0"`. Compare them with `"1"`.
- **Windows support is less tested than macOS.** The original README marked Windows as unsupported, although the source has a Windows implementation and a 64-bit Windows binary was shipped. Check the results on your own hardware before relying on them.
- **Preemptive processes:** the manifest doesn't declare the command thread-safe, so expect 4D to treat it as unusable in preemptive processes.

---

## DEVICES LIST

### Syntax

```
DEVICES LIST ( names ; types ; vendors ; products ; writesCD ; writesDVD )
```

| Parameter | Type | Description |
|---|---|---|
| `names` | Text array | **On macOS**: the device's display name. **On Windows**: the drive letter with its colon, such as `"D:"` |
| `types` | Text array | **On macOS**: the physical interconnect (the bus the drive is attached to, as DiscRecording reports it). **On Windows**: `"DVD"` or `"CD"` |
| `vendors` | Text array | **On macOS**: the vendor name. **On Windows**: always `""` |
| `products` | Text array | **On macOS**: the product name. **On Windows**: always `""` |
| `writesCD` | Text array | **On macOS**: `"1"` if the drive can write CDs, `"0"` otherwise. **On Windows**: always `""` |
| `writesDVD` | Text array | **On macOS**: `"1"` if the drive can write DVDs, `"0"` otherwise. **On Windows**: always `""` |
| Result | (none) | The command returns its results in the six arrays |

### Description

`DEVICES LIST` replaces the contents of the six arrays. Each array gets one element per drive found, and element `i` of every array describes the same drive. Element 0 is empty.

Pass `Text` arrays (`ARRAY TEXT`) or undefined variables; an undefined variable becomes a `Text` array. A variable that is already an array of another type (for example a Longint array) is left unchanged, without an error.

**On macOS**, the list is whatever `[DRDevice devices]` returns at the time of the call: the optical drives DiscRecording knows about, built in or external (for example a USB SuperDrive). The vendor, product and interconnect values come from the device's info dictionary. If DiscRecording has no value for one of them, the element holds the literal text `"(null)"`, not `""`. If a device has no display name, the first array holds `""` for it, so the six arrays stay aligned. That fallback exists only from version 14.3.0 of this repository; before it, the name was skipped and the first array could get out of step with the others.

**On Windows**, the plugin enumerates the drive letters (`GetLogicalDriveStrings`), opens each volume and asks the storage driver for its device type (`IOCTL_STORAGE_GET_MEDIA_TYPES_EX`). A drive that reports a DVD device type is listed as `"DVD"`, one that reports a CD-ROM device type as `"CD"`; every other drive is skipped. A drive that can't be opened, or whose driver doesn't answer that request, is skipped silently. Only drives with a drive letter are found. Vendor and product names and writing capabilities are not available on Windows.

### Example

From the plugin's original README (the same calls appear in the test database `Devices.4dbase`):

```4d
PLATFORM PROPERTIES($platform)

Case of 
: ($platform=Mac OS)

DEVICES LIST (\
$displayNames;\
$physicalInterconnects;\
$vendorNames;\
$productNames;\
$writesCDs;\
$writesDVDs)

: ($platform=Windows)

DEVICES LIST (\
$logicalDriveStrings;\
$mediaType)

End case 
```

The Windows branch above passes only two arrays. Pass all six instead, on both platforms:

```4d
ARRAY TEXT($names;0)
ARRAY TEXT($types;0)
ARRAY TEXT($vendors;0)
ARRAY TEXT($products;0)
ARRAY TEXT($writesCD;0)
ARRAY TEXT($writesDVD;0)

DEVICES LIST($names;$types;$vendors;$products;$writesCD;$writesDVD)

If (Size of array($names)=0)
	ALERT("No CD/DVD drive found.")
Else 
	For ($i;1;Size of array($names))
		ALERT($names{$i}+" ("+$types{$i}+")")
	End for 
End if 
```

List the drives that can write DVDs (macOS only; on Windows the capability arrays are empty):

```4d
ARRAY TEXT($names;0)
ARRAY TEXT($types;0)
ARRAY TEXT($vendors;0)
ARRAY TEXT($products;0)
ARRAY TEXT($writesCD;0)
ARRAY TEXT($writesDVD;0)

DEVICES LIST($names;$types;$vendors;$products;$writesCD;$writesDVD)

ARRAY TEXT($burners;0)
For ($i;1;Size of array($names))
	If ($writesDVD{$i}="1")
		APPEND TO ARRAY($burners;$vendors{$i}+" "+$products{$i})
	End if 
End for 
```

---

## Error handling & troubleshooting

- **Empty arrays:** no optical drive is attached, or (on Windows) none of the drives could be opened or reported a CD/DVD device type. The command doesn't tell these cases apart and raises no error.
- **`"(null)"` in the type, vendor or product arrays (macOS):** DiscRecording had no value for that property of the device. Treat it as unknown.
- **An array didn't change:** it was already an array of a type other than `Text`. Declare it with `ARRAY TEXT`, or pass an undefined variable.
- **A drive is missing on Windows:** only drives with a drive letter are checked. Opening the volume can also be refused, for example by a policy that restricts CD-ROM access; such a drive is skipped silently (inferred from the code path, not tested).
- **Empty capability arrays on Windows:** expected. Only the drive letter and the `"CD"`/`"DVD"` type are available there.

---

## Quick reference

```4d
ARRAY TEXT($names;0)
ARRAY TEXT($types;0)
ARRAY TEXT($vendors;0)
ARRAY TEXT($products;0)
ARRAY TEXT($writesCD;0)
ARRAY TEXT($writesDVD;0)
DEVICES LIST($names;$types;$vendors;$products;$writesCD;$writesDVD)
// macOS:   $names = display name, $types = interconnect, $writesCD/$writesDVD = "1"/"0"
// Windows: $names = "D:", $types = "CD"/"DVD", other arrays = ""
```
