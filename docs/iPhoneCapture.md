# Capturing FlaschenTaschen with an iPhone

RGB LED panels use PWM (Pulse Width Modulation) to control brightness. When a camera's shutter speed or frame rate doesn't align with the panel's refresh cycle, partial PWM cycles are captured, producing dark rolling bands. This guide explains how to avoid that.

## Step 1: Set the Panel Refresh Rate

Run the server with a refresh rate that is a clean multiple of your target camera frame rate. `120 Hz` works for 24, 30, and 60 fps.

```
--led-limit-refresh=120 --led-pwm-bits=8
```

Leave busy-waiting **on** (the default) for stable timing precision.

## Step 2: Use a Third-Party Camera App

The iPhone's built-in Camera app uses auto-exposure and will often choose a shutter speed that mismatches the panel refresh rate. Use an app that allows manual shutter speed control:

- **Halide Mark II** — recommended, straightforward manual controls
- **ProCamera** — full manual exposure

## Step 3: Set Shutter Speed Manually

Set the shutter speed to **1/120s or slower**. This ensures the exposure spans at least one complete panel refresh cycle, averaging out any PWM variation.

| Panel refresh rate | Minimum shutter speed |
|---|---|
| 120 Hz | 1/120s |
| 90 Hz | 1/90s |
| 60 Hz | 1/60s |

Slower shutter speeds (1/60s, 1/30s) work even better for static or slow-moving content since they capture multiple full cycles.

## Step 4: Compensate for Exposure

A slower shutter speed lets in more light. To avoid overexposure:

- Lower ISO to the minimum (ISO 32 or 50 on recent iPhones)
- If the display is very bright, add distance or angle the shot slightly

## For Video Recording

Set the panel refresh rate to match the recording frame rate:

| iPhone video mode | Frame rate | `--led-limit-refresh` |
|---|---|---|
| Standard | 30 fps | `120` |
| High frame rate | 60 fps | `120` or `180` |
| Cinematic | 24 fps | `120` |
| Slo-mo | 120 fps | `120` or `240` |

In **Settings → Camera → Record Video**, lock the frame rate to a specific mode rather than using Auto.
