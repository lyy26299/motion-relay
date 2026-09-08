# Images Directory

## Required Images

### demo-preview.png
**Purpose**: Main demo screenshot for README.md

**Requirements**:
- Resolution: 1600x900 or similar 16:9 aspect ratio
- Format: PNG
- Content: Show the AI coach analyzing a workout
- Should include:
  - Video feed with person doing exercise
  - AI feedback visible
  - Clean, professional look

**How to capture**:
1. Run the agent: `./scripts/run.sh`
2. Join the video call
3. Start doing an exercise (squat, push-up, etc.)
4. Take screenshot while AI is giving feedback
5. Save as `demo-preview.png` in this directory

**Example layout**:
```
┌─────────────────────────────────────────┐
│  Vision Agent Demo                      │
├─────────────────────────────────────────┤
│  [Video Feed]     │  [AI Feedback]      │
│   Person doing    │                     │
│   squat with      │  "Good depth!       │
│   skeleton        │   Watch your knees" │
│   overlay         │                     │
└─────────────────────────────────────────┘
```

---

## Optional Images

### demo.gif
- Animated demo showing 5-10 seconds of workout
- Shows AI giving real-time feedback
- Max size: 10MB

### architecture.png
- System architecture diagram
- Shows data flow from camera to AI

### exercises/ directory
- Screenshots of each exercise
- Shows proper form
- Optional but nice to have

---

## Placeholder

Until you add actual images, the README will show a broken image icon.
This is expected and can be fixed after publishing.
