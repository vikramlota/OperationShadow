# Physical Movement Workflow - Layer by Layer

Quick reference showing what happens at each layer.

---

## Layer 1: User Interface (Browser/API)

### Keyboard Input Path
```
User presses W key
    ↓
JavaScript keyboard event listener triggers
    ↓
Payload: {"type": "command", "command": "W"}
    ↓
Sent via WebSocket to /ws/scout/control
    ↓
Size: ~40 bytes (JSON)
```

### Frontend Code: `frontend/src/components/ScoutControl.tsx`
```typescript
// Listen for keyboard
document.addEventListener('keydown', (e) => {
  if (e.key === 'w') {
    ws.send(JSON.stringify({
      type: 'command',
      command: 'W'
    }));
  }
});

// On key release
document.addEventListener('keyup', (e) => {
  if (e.key === 'w') {
    ws.send(JSON.stringify({
      type: 'command',
      command: 'X'  // Auto-stop on release
    }));
  }
});
```

---

## Layer 2: WebSocket Transport

### Connection Establishment
```
Browser opens: ws://localhost:8000/ws/scout/control
    ↓
Backend accepts connection
    ↓
Initial status sent to browser
    ↓
Browser ready to send commands
```

### Data Format
```
Direction: Browser → Backend
Message type: Text (JSON)
Payload: {"type": "command|keepalive", "command": "W|A|S|D|X"}
Latency: 1-5ms
```

### Backend Code: `backend/fastapi/api/scout_control.py`
```python
@websocket_router.websocket("/ws/scout/control")
async def scout_control_websocket(websocket: WebSocket):
    await websocket.accept()
    
    # Get the simulator (manages all control state)
    simulator = getattr(websocket.app.state, "scout_control_simulator")
    
    # Track this client
    client_id = await simulator.connect_client()
    
    try:
        # Initial status
        await websocket.send_json(await simulator.status())
        
        # Receive loop
        while True:
            raw_frame = await websocket.receive_text()
            frame = ControlFrame.model_validate_json(raw_frame)
            
            if frame.type == "command":
                success = await simulator.update_command(client_id, frame.command)
                if not success:
                    await websocket.send_json({"type": "error", ...})
            
            elif frame.type == "keepalive":
                await simulator.keepalive(client_id)
    
    finally:
        await simulator.disconnect_client(client_id)
```

---

## Layer 3: Backend Command Routing

### Request Validation
```
Raw JSON string received
    ↓
Pydantic validates: ControlFrame.model_validate_json()
    ↓
Extract: type="command", command="W"
    ↓
Check constraints:
    - command must be in {W, A, S, D, X}
    - type must be in {command, keepalive}
    ↓
Pass to ScoutControlSimulator
```

### Backend Code: `backend/fastapi/api/scout_control.py`
```python
class ControlFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")  # Reject unknown fields
    
    type: Literal["command", "keepalive"]
    command: Literal["W", "A", "S", "D", "X"] | None = None

# Validation happens automatically
frame = ControlFrame.model_validate_json(raw_frame)
```

---

## Layer 4: Lease & Session Management

### ScoutControlSimulator State

#### File: `backend/fastapi/services/scout_control_simulator.py`

```python
class ScoutControlSimulator:
    def __init__(self, serial_writer: ScoutSerialWriter | None = None):
        self._lease_owner: str | None = None          # Who has control?
        self._lease_expiry: float | None = None       # When does lease expire?
        self._current_command: str = "X"              # What's being executed?
        self._active_clients: dict[str, float] = {}   # Connected clients
        self._serial_writer: ScoutSerialWriter = serial_writer
        self._gateway: DashboardGateway = gateway
```

### Lease Acquisition Flow
```
Browser sends command for client_id_123
    ↓
Check: Does client_123 have active lease?
    ├─ YES → Update current_command, send to serial
    └─ NO → Check if lease expired
       ├─ Expired → Grant new lease
       └─ Not expired → Reject (another client owns it)
    ↓
Set: _lease_owner = client_id_123
Set: _lease_expiry = now + 5_seconds
```

### Code Example
```python
async def update_command(self, client_id: str, command: str) -> bool:
    """Update command only if client has active lease"""
    
    # Check if client has lease
    if self._lease_owner and self._lease_owner != client_id:
        # Check if lease expired
        if time.time() < self._lease_expiry:
            # Another client still owns it
            return False
    
    # Grant/update lease
    self._lease_owner = client_id
    self._lease_expiry = time.time() + 5.0
    self._current_command = command
    
    # Forward to hardware
    await self._serial_writer.send(command)
    
    # Broadcast to dashboard
    await self._gateway.broadcast_status()
    
    return True
```

---

## Layer 5: Safety Gate System

### 4 Independent Safety Gates

```
┌─ Gate 1: SHADOW_SCOUT_SERIAL_ENABLED ─┐
│  (Is serial output enabled in config?)  │
├─────────────────────────────────────────┤
│ Gate 2: SHADOW_HARDWARE_LOCKOUT        │
│  (Is hardware control lock active?)     │
├─────────────────────────────────────────┤
│ Gate 3: SHADOW_DEFAULT_VEHICLE_MODE    │
│  (Is vehicle in real or simulated mode?)│
├─────────────────────────────────────────┤
│ Gate 4: SHADOW_SCOUT_SERIAL_FAILSAFE   │
│  (Is USB-loss failsafe verified?)       │
└─ All must be open for movement ────────┘
```

### Gate Check Code

```python
# backend/fastapi/services/scout_serial_writer.py

def __init__(
    self,
    *,
    enabled: bool,              # Gate 1
    hardware_lockout: bool,     # Gate 2
    real_mode: bool,            # Gate 3
    failsafe_verified: bool,    # Gate 4
):
    # All 4 must be true to arm
    self.armed = (
        enabled 
        and not hardware_lockout 
        and real_mode 
        and failsafe_verified
    )

async def send(self, command: str) -> None:
    if not self.armed:
        logger.info("Serial writes remain disabled by gates")
        return
    
    # Gates passed, proceed...
```

### Configuration Values

```bash
# .env file

# Test Mode (Development)
SHADOW_HARDWARE_LOCKOUT=false          # Gate 1: Allow
SHADOW_DEFAULT_VEHICLE_MODE=real       # Gate 3: Real mode
SHADOW_SCOUT_SERIAL_ENABLED=true       # Gate 2: Enable serial
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true  # Gate 4: Verified

# Safe Mode (Default)
SHADOW_HARDWARE_LOCKOUT=true           # Gate 1: Block
SHADOW_DEFAULT_VEHICLE_MODE=simulated  # Gate 3: Simulated
SHADOW_SCOUT_SERIAL_ENABLED=false      # Gate 2: Disable serial
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false  # Gate 4: Not verified
```

---

## Layer 6: Emergency Stop Latch

### Global E-Stop State

```python
# Global variables
_GLOBAL_ESTOP_LATCHED: bool = False
_GLOBAL_ESTOP_SIGNALS: set[asyncio.Event] = set()

def latch_global_emergency_stop() -> None:
    """Latch E-Stop and wake all waiting tasks"""
    global _GLOBAL_ESTOP_LATCHED
    _GLOBAL_ESTOP_LATCHED = True
    for ev in list(_GLOBAL_ESTOP_SIGNALS):
        ev.set()  # Wake up any waiting task

def reset_global_emergency_stop(*, authorized: bool = False) -> None:
    """Only reset with explicit authorization"""
    if not authorized:
        raise PermissionError("Authorization required")
    _GLOBAL_ESTOP_LATCHED = False
```

### Double-Check Pattern

```python
async def send(self, command: str) -> None:
    # Check 1: Before acquiring lock (fast path)
    if command != "X" and self.emergency_stop_latched:
        raise EmergencyStopLatchedError(...)
    
    # Acquire lock
    async with self._lock:
        # Check 2: Inside lock (no race condition)
        if command != "X" and self.emergency_stop_latched:
            raise EmergencyStopLatchedError(...)
        
        # Safe to proceed
        await self._connection.write(command.encode("ascii"))
```

### Why Double-Check?

```
Without double-check:
  Thread 1: Check E-Stop (not latched) → proceeds
  Thread 2: Latch E-Stop globally
  Thread 1: Acquires lock and sends 'W' → PROBLEM!

With double-check:
  Thread 1: Check E-Stop (not latched) → tries to proceed
  Thread 2: Latch E-Stop globally
  Thread 1: Re-check inside lock → blocked! → Sends 'X' instead
```

---

## Layer 7: Serial Port Connection

### USB Port Detection

```python
async def ensure_connected(self) -> bool:
    """Find and connect to ESP32 on any USB port"""
    
    if not self.armed:
        return False
    
    if self.connected:
        return True  # Already connected
    
    # Try ports in order
    candidate_ports = [
        self.port,                 # Primary (from .env)
        "/dev/cu.usbmodem14201",  # Common ESP32
        "/dev/cu.usbmodem14301",
        "/dev/cu.usbserial*",     # Some CH340 adapters
        "/dev/ttyUSB0",           # Linux fallback
    ]
    
    for candidate in candidate_ports:
        try:
            self._connection = serial.Serial(
                port=candidate,
                baudrate=115200,
                timeout=0.1,
                write_timeout=0.5
            )
            logger.info(f"Connected to {candidate}")
            return True
        except serial.SerialException:
            continue
    
    logger.warning("No ESP32 found on any port")
    return False
```

### Connection Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| Baudrate | 115200 | Must match ESP32 firmware |
| Data bits | 8 | Standard ASCII |
| Stop bits | 1 | Standard |
| Parity | None | No error checking |
| Timeout | 0.1s | Read timeout (non-blocking) |
| Write timeout | 0.5s | Write must complete quick |

---

## Layer 8: Serial Write (The Critical Moment)

### Atomic Write Sequence

```python
async def send(self, command: str) -> None:
    """Send single ASCII byte to ESP32 at 115200 baud"""
    
    # 1. Validate
    if command not in {"W", "A", "S", "D", "X"}:
        raise ValueError(...)
    
    # 2. Check E-Stop (outside lock - fast path)
    if command != "X" and self.emergency_stop_latched:
        raise EmergencyStopLatchedError(...)
    
    # 3. Acquire lock (ensure only one write at a time)
    async with self._lock:
        # 4. Re-check E-Stop (inside lock - safe)
        if command != "X" and self.emergency_stop_latched:
            raise EmergencyStopLatchedError(...)
        
        # 5. Verify connection
        if self._connection is None or not self._connection.is_open:
            if command == "X":
                self.stop_fault_count += 1
                raise SerialStopCommandFaultError(...)
            raise SerialException("Transmitter offline")
        
        # 6. Write byte
        written = await asyncio.to_thread(
            self._connection.write,
            command.encode("ascii")  # "W" → b'\x57'
        )
        
        if written != 1:
            raise SerialException(f"Short write: {written}")
        
        # 7. Flush buffer
        await asyncio.to_thread(self._connection.flush)
        
        logger.info(f"✓ Sent '{command}' (0x{ord(command):02x})")
```

### Physical Data on Wire

```
Sending command 'W' (ASCII 87, 0x57, binary 01010111)

Transmission @ 115200 baud:
  1 bit every 8.68 microseconds
  10 bits per character (1 start + 8 data + 1 stop)
  Total time: ~86.8 microseconds

UART Frame:
  [Start bit]
  01010111  ← ASCII byte W
  [Stop bit]
  
Serial monitor would display: "W"
```

---

## Layer 9: Movement Execution with Duration

### Complete Movement Cycle

```python
async def execute_step(
    self,
    command: str,        # W (forward), A (left), S (back), D (right)
    duration_s: float,   # How long to execute
    pause_s: float = 0.5 # Settle time after
) -> bool:
    """Execute movement for duration, then stop"""
    
    # Pre-check
    if command != "X" and self.emergency_stop_latched:
        await self._safe_stop_on_abort()
        return False
    
    # Ensure connected
    if not await self.ensure_connected():
        return False
    
    try:
        # Send movement command
        await self.send(command)  # e.g., send('W')
        
        # Wait for duration (interruptible by E-Stop)
        if duration_s > 0:
            completed = await wait_unless_emergency_stopped(duration_s)
            if not completed:
                # E-Stop was triggered mid-movement
                await self.send_verified_stop()
                return False
        
        # Send stop command
        await self.send_verified_stop()  # send('X')
        
        # Wait for settle/pause
        if pause_s > 0:
            await wait_unless_emergency_stopped(pause_s)
        
        return True
    
    except EmergencyStopLatchedError:
        await self._safe_stop_on_abort()
        return False
```

### Timing Diagram

```
T=0.00s: execute_step(command='W', duration_s=0.5)
         └─ send('W')
T=0.01s: 'W' byte on wire
T=0.50s: duration expired
         └─ send_verified_stop()
T=0.51s: 'X' byte on wire
T=0.52s: await pause_s
T=1.02s: return True (movement complete)
```

---

## Layer 10: ESP32 Receives Command

### UART Reception

```
ESP32 UART peripheral receives bits
    ↓
Reconstructs byte: 0x57 (ASCII 'W')
    ↓
Triggers ISR (interrupt)
    ↓
Firmware code: 
    byte = uart.read()
    if (byte == 'W'):
        motor_controller.forward()
```

### ESP32 Motor Control

```cpp
void handle_serial_command(char byte) {
    switch(byte) {
        case 'W':  // Forward
            motor_left.set_direction(FORWARD);
            motor_right.set_direction(FORWARD);
            motor_left.set_speed(100);
            motor_right.set_speed(100);
            break;
        
        case 'A':  // Turn left
            motor_left.set_speed(50);
            motor_right.set_speed(100);
            break;
        
        case 'S':  // Backward
            motor_left.set_direction(BACKWARD);
            motor_right.set_direction(BACKWARD);
            motor_left.set_speed(100);
            motor_right.set_speed(100);
            break;
        
        case 'D':  // Turn right
            motor_left.set_speed(100);
            motor_right.set_speed(50);
            break;
        
        case 'X':  // Stop
            motor_left.set_speed(0);
            motor_right.set_speed(0);
            break;
    }
    
    // Encode in ESP-NOW packet
    esp_now_packet.motor_left = motor_left.get_speed();
    esp_now_packet.motor_right = motor_right.get_speed();
    esp_now_packet.sequence = ++seq_num;
    
    // Send to Scout receiver via 2.4GHz
    esp_now_send(scout_receiver_mac, &esp_now_packet);
}
```

---

## Layer 11: Scout Receiver

### ESP-NOW Reception

```
Scout receiver detects RF packet on 2.4GHz
    ↓
Validates CRC
    ↓
Triggers ESP-NOW callback
    ↓
Firmware extracts motor commands:
    left_speed = packet.motor_left
    right_speed = packet.motor_right
    ↓
Apply to motor drivers
```

### Motor Driver Control

```cpp
void update_motors(int left_speed, int right_speed) {
    // Convert speed (-100 to +100) to PWM (0 to 255)
    analogWrite(MOTOR_LEFT_PWM, map(left_speed, -100, 100, 0, 255));
    analogWrite(MOTOR_RIGHT_PWM, map(right_speed, -100, 100, 0, 255));
    
    // Set direction pins
    digitalWrite(MOTOR_LEFT_DIR, left_speed > 0 ? HIGH : LOW);
    digitalWrite(MOTOR_RIGHT_DIR, right_speed > 0 ? HIGH : LOW);
}
```

---

## Layer 12: Physical Motors

### Motor Spin-Up

```
PWM duty cycle increases
    ↓
Motor coils energize
    ↓
Magnetic field builds
    ↓
Rotor experiences torque
    ↓
Motor accelerates
    ↓
Wheels rotate
    ↓
🚗 Scout moves!
```

---

## Layer 13: Status Feedback

### Dashboard Update Loop

```python
# After each command, broadcast status
async def broadcast_status(self):
    status = {
        "type": "status",
        "mode": "real",                    # vs "simulated"
        "simulation_available": False,     # Can toggle mode?
        "transmitter_connected": True,     # ESP32 online?
        "lease_owner": client_id_123,      # Who has control?
        "current_command": "W",            # What's executing?
        "lease_expires_at": timestamp      # When does lease expire?
    }
    
    await self._gateway.broadcast_json(status)
```

### Frontend Display

```javascript
// Receive status update
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  
  if (message.type === 'status') {
    // Update display
    document.getElementById('mode').textContent = message.mode;
    document.getElementById('transmitter').textContent = 
      message.transmitter_connected ? '✓ CONNECTED' : '✗ OFFLINE';
    document.getElementById('current_command').textContent = 
      message.current_command;
  }
});
```

---

## Summary: Command Journey

```
Browser                →  /ws/scout/control
                           ↓
WebSocket Message      →  JSON deserialization
                           ↓
Control Frame          →  Lease verification
                           ↓
Simulator              →  Safety gates check
                           ↓
Serial Writer          →  E-Stop double-check
                           ↓
USB Port               →  115200 baud transmission
                           ↓
b'W'                   →  ~87 microseconds
                           ↓
ESP32 UART             →  Motor control
                           ↓
ESP-NOW                →  2.4GHz RF packet
                           ↓
Scout Receiver         →  Motor driver update
                           ↓
Motor PWM              →  Wheel spin-up
                           ↓
⚙️ Physical Motion    ←  ~60-120ms total latency
                           ↓
Status Feedback        →  WebSocket to dashboard
                           ↓
Browser Display        →  User sees result
```

