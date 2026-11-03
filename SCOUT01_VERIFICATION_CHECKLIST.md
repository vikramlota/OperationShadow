# SCOUT01 Verification Checklist

Now that you have SSH access to SCOUT01 (`ssh <ROVER_USER>@<SCOUT_IP>`), run these commands to verify the rover is ready for Operation Shadow integration.

---

## **1. System Information**

```bash
# Check OS and kernel
uname -a

# Check available disk space
df -h

# Check memory
free -h

# Check running processes
top -bn1 | head -20
```

---

## **2. ROS 2 Installation & Status**

```bash
# Check ROS 2 version
ros2 --version

# List active ROS 2 nodes
ros2 node list

# List available ROS 2 topics
ros2 topic list

# Check if rosbridge_server is running
ros2 node list | grep rosbridge

# Check rosbridge_server process
ps aux | grep rosbridge

# If rosbridge is not running, check how to start it:
systemctl status rosbridge
# OR
ps aux | grep ros2
```

---

## **3. Camera Stream (MJPEG Server)**

```bash
# Check if MJPEG stream server is running
ps aux | grep mjpeg
# OR
ps aux | grep stream

# Check what's listening on port 8081
sudo netstat -tlnp | grep 8081
# OR
sudo lsof -i :8081

# Test camera stream locally
curl -I http://localhost:8081/stream.mjpg
```

---

## **4. Network Configuration**

```bash
# Verify SCOUT01 IP address
ip addr show | grep "inet <SUBNET>"

# Check firewall rules
sudo ufw status

# Verify ports are open
sudo netstat -tlnp | grep -E ':(8081|9090|22)'
```

---

## **5. Serial Port (if using direct serial communication)**

```bash
# Check if serial device exists
ls -l /dev/ttyUSB* /dev/ttyACM*

# Check serial port permissions
groups <ROVER_USER>  # Should include 'dialout' group

# Add user to dialout group if needed
sudo usermod -a -G dialout <ROVER_USER>
```

---

## **6. ROS 2 Topic Inspection**

```bash
# Check cmd_vel topic (movement commands)
ros2 topic info /scout/cmd_vel
ros2 topic echo /scout/cmd_vel --once

# Check odometry topic (position feedback)
ros2 topic info /scout/odom
ros2 topic echo /scout/odom --once

# Check battery topic (if available)
ros2 topic list | grep battery
ros2 topic echo /scout/battery --once

# Check camera topics
ros2 topic list | grep -i camera
ros2 topic list | grep -i image
```

---

## **7. Expected ROS 2 Topics for Operation Shadow**

The backend expects these topics (verify they exist):

```bash
# Movement commands (Backend publishes here)
ros2 topic info /scout/cmd_vel

# Position/velocity feedback (Backend subscribes)
ros2 topic info /scout/odom

# Battery status (Backend subscribes)
ros2 topic info /scout/battery

# Camera feed metadata (optional)
ros2 topic info /oak/rgb/image_raw
```

---

## **8. Start/Restart Services (if needed)**

```bash
# If rosbridge_server is not running:
ros2 launch rosbridge_server rosbridge_websocket_launch.xml
# OR
ros2 run rosbridge_server rosbridge_websocket

# If MJPEG stream is not running:
# (Check SCOUT01's launch files or systemd services)
systemctl status scout-camera-stream
# OR find the camera stream process:
ps aux | grep camera
```

---

## **9. Test Commands from SCOUT01**

```bash
# Test sending a movement command
ros2 topic pub --once /scout/cmd_vel geometry_msgs/msg/Twist "{linear: {x: 0.0, y: 0.0, z: 0.0}, angular: {x: 0.0, y: 0.0, z: 0.0}}"

# Test reading odometry
ros2 topic echo /scout/odom --once

# Test camera stream is accessible
curl -s http://localhost:8081/stream.mjpg | head -c 100
```

---

## **10. Log Inspection**

```bash
# Check ROS 2 logs
ros2 daemon status
ls -lah ~/.ros/log/

# Check system logs for errors
journalctl -u rosbridge -n 50
journalctl -u scout-camera* -n 50

# Check for recent errors
dmesg | tail -50
```

---

## **Expected Output Summary**

After running these commands, you should see:

✅ **ROS 2 installed** (e.g., `ros2 --version` shows Humble/Iron/Jazzy)
✅ **rosbridge_server running** (port 9090 listening, node appears in `ros2 node list`)
✅ **Camera stream running** (port 8081 listening, returns MJPEG data)
✅ **Topics exist**: `/scout/cmd_vel`, `/scout/odom`, `/scout/battery` (or similar)
✅ **Firewall allows** ports 8081, 9090, 22 from control laptop IP
✅ **Network interface** shows <SCOUT_IP>

---

## **Troubleshooting**

### rosbridge_server not running
```bash
# Start manually:
ros2 launch rosbridge_server rosbridge_websocket_launch.xml

# Or add to startup:
sudo systemctl enable rosbridge
sudo systemctl start rosbridge
```

### Camera stream not working
```bash
# Find the camera launch file
find ~ -name "*camera*.launch*" -o -name "*oak*.launch*"

# Launch manually:
ros2 launch <package_name> <launch_file>
```

### Topics have different names
```bash
# List all topics and identify the correct ones:
ros2 topic list

# Update backend configuration to match actual topic names
```

---

## **Report Back**

After running these checks, report:

1. **ROS 2 version**: _________
2. **rosbridge_server status**: Running / Not Running
3. **Camera stream status**: Working / Not Working
4. **Key topics found**:
   - cmd_vel: _________
   - odom: _________
   - battery: _________
5. **Any errors or warnings**: _________

---

**Next Step**: Once verified, start the Operation Shadow backend and dashboard to test full integration.
