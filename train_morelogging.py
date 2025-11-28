import asyncio
import time
from bleak import BleakScanner, BleakClient

# LEGO Powered Up Wireless Protocol UUID (same for write + notify)
UART_UUID = "00001624-1212-efde-1623-785feabcd123"

TARGET_NAME = "HUB NO.4"   # what your train hub advertises as
HARDCODED_ADDRESS = None   # e.g. "90:84:2B:0D:18:37" if you want to skip scanning


# ----------------------------
# Small helper: map color name -> LED code
# ----------------------------
LED_COLORS = {
    "off":   0x00,
    "pink":  0x01,
    "purple":0x02,
    "blue":  0x03,
    "light_blue": 0x04,
    "cyan":  0x05,
    "green": 0x06,
    "yellow":0x07,
    "orange":0x08,
    "white": 0x09,
    "red":   0x0A,
}


class TrainHub:
    """
    High-level wrapper around a LEGO Powered Up City/Train hub ("HUB NO.4").
    
    Enhanced diagnostic version with comprehensive logging.

    Features:
    - Connect / disconnect
    - Subscribe to notifications
    - Background heartbeat to keep the connection alive
    - set_speed(), stop(), set_led(), run_show()
    - Detailed logging and diagnostics
    """

    def __init__(self, address=None):
        self.address = address
        self.client: BleakClient | None = None
        self._heartbeat_task: asyncio.Task | None = None

        # state
        self._desired_speed = 0       # -100..100
        self._running = False         # controls heartbeat loop
        self._initialized = False     # hub ready for commands
        self._speed_was_set = False   # track if we've set speed explicitly
        
        # Track port discovery
        self._ports_discovered = set()
        self._port_discovery_complete = asyncio.Event()
        self._last_command_time = 0   # Track command timing
        self._connection_start_time = 0  # For timestamp calculations

    # ----------------------------
    # Discovery / connection
    # ----------------------------
    @staticmethod
    async def discover_address():
        """Find the first HUB NO.4 in range and return its address."""
        print("Scanning for HUB NO.4...")
        devices = await BleakScanner.discover(timeout=6.0)
        for d in devices:
            name = d.name or ""
            if TARGET_NAME in name:
                print(f"Found hub: {name} [{d.address}]")
                return d.address
        print("No hub found. Make sure it's on and blinking.")
        return None

    async def connect(self):
        """Connect to the hub and start heartbeat + notifications."""
        if self.address is None:
            if HARDCODED_ADDRESS:
                print(f"Using hardcoded address: {HARDCODED_ADDRESS}")
                self.address = HARDCODED_ADDRESS
            else:
                self.address = await self.discover_address()
                if self.address is None:
                    return False

        print(f"\n{'='*60}")
        print(f"Connecting to {self.address} ...")
        print(f"{'='*60}")
        self.client = BleakClient(self.address)

        try:
            await self.client.connect(timeout=20.0)
        except Exception as e:
            print("Connect failed:", repr(e))
            self.client = None
            return False

        if not self.client.is_connected:
            print("Connect reported success but client.is_connected is False.")
            self.client = None
            return False

        self._connection_start_time = time.time()
        print("✓ BLE connection established")
        print("\nSubscribing to notifications...")
        await self.client.start_notify(UART_UUID, self._notification_handler)
        
        print("✓ Notification subscription active")
        print("\nWaiting for port discovery (expect 4 ports)...")
        print("  Expected: 0x00 (motor), 0x32 (LED), 0x3B (current), 0x3C (voltage)")
        
        # Wait for port discovery (with timeout)
        try:
            await asyncio.wait_for(self._port_discovery_complete.wait(), timeout=5.0)
            print("\n✓ Port discovery complete!")
        except asyncio.TimeoutError:
            print(f"\n✗ Timeout! Only discovered {len(self._ports_discovered)} ports: {self._ports_discovered}")
            await self.disconnect()
            return False

        self._initialized = True
        
        # Now start the heartbeat
        self._running = True
        print("\nStarting heartbeat loop...")
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        await asyncio.sleep(0.3)
        
        print("\n✓ Hub ready for commands")
        print(f"{'='*60}\n")
        return True

    async def disconnect(self):
        """Stop heartbeat and cleanly disconnect."""
        self._running = False
        if self._heartbeat_task:
            try:
                await self._heartbeat_task
            except Exception as e:
                print("Heartbeat task ended with error:", repr(e))
            self._heartbeat_task = None

        if self.client and self.client.is_connected:
            print("Disconnecting from hub...")
            try:
                await self.client.stop_notify(UART_UUID)
            except Exception:
                pass
            try:
                await self.client.disconnect()
            except Exception as e:
                print("Disconnect error:", repr(e))

        self.client = None
        print("Disconnected.")

    # ----------------------------
    # Notification handler
    # ----------------------------
    def _notification_handler(self, _char_handle: int, data: bytearray):
        """
        Called whenever the hub sends us a message.
        Enhanced logging to understand hub responses.
        """
        timestamp = time.time() - self._connection_start_time
        hex_str = " ".join(f"{b:02X}" for b in data)
        print(f"[{timestamp:6.3f}s] RX: {hex_str}")
        
        # Decode message type
        if len(data) >= 3:
            msg_type = data[2]
            
            if msg_type == 0x04:  # Port Information
                port_id = data[3]
                event = data[4]  # 0x01 = attached, 0x00 = detached
                
                if event == 0x01:
                    io_type = data[5] if len(data) > 5 else 0
                    io_type_names = {
                        0x02: "Train Motor",
                        0x17: "RGB LED",
                        0x14: "Voltage Sensor",
                        0x15: "Current Sensor"
                    }
                    type_name = io_type_names.get(io_type, f"Unknown(0x{io_type:02X})")
                    
                    self._ports_discovered.add(port_id)
                    print(f"  → Port 0x{port_id:02X} attached: {type_name} ({len(self._ports_discovered)}/4)")
                    
                    if len(self._ports_discovered) >= 4:
                        print("  → All ports discovered!")
                        self._port_discovery_complete.set()
                elif event == 0x00:
                    print(f"  → Port 0x{port_id:02X} detached")
                    
            elif msg_type == 0x05:  # Generic Error
                print(f"  → ERROR: {hex_str}")
                
            elif msg_type == 0x82:  # Port Output Command Feedback
                port_id = data[3]
                feedback = data[4] if len(data) > 4 else 0
                feedback_names = {
                    0x01: "Buffer Empty (can send more)",
                    0x02: "Buffer Full",
                    0x04: "Current Command Completed",
                    0x08: "Current Command Discarded",
                    0x10: "Idle (motor stopped)"
                }
                fb_str = feedback_names.get(feedback, f"Unknown(0x{feedback:02X})")
                print(f"  → Port 0x{port_id:02X} feedback: {fb_str}")

    # ----------------------------
    # Heartbeat
    # ----------------------------
    async def _heartbeat_loop(self):
        """
        Wait for port discovery to complete, then send periodic speed commands.
        """
        print("Heartbeat loop active - sending speed commands.")
        interval = 0.1  # 100 ms
        
        while self._running and self.client and self.client.is_connected:
            try:
                # Send the current speed
                await self._send_speed_command(self._desired_speed)
            except Exception as e:
                print("Heartbeat write failed:", repr(e))
                self._running = False
                break
            
            await asyncio.sleep(interval)

        print("Heartbeat loop exiting.")

    # ----------------------------
    # Low-level commands with logging
    # ----------------------------
    async def _send_command_with_logging(self, cmd: bytearray, description: str):
        """Send a command and log it with timing."""
        if not self.client or not self.client.is_connected:
            print(f"  ✗ Cannot send {description}: Not connected")
            raise RuntimeError("Not connected")
        
        timestamp = time.time() - self._connection_start_time
        hex_str = " ".join(f"{b:02X}" for b in cmd)
        print(f"[{timestamp:6.3f}s] TX: {hex_str} ({description})")
        
        try:
            await self.client.write_gatt_char(UART_UUID, cmd)
            self._last_command_time = time.time()
            print(f"  ✓ {description} sent successfully")
        except Exception as e:
            print(f"  ✗ {description} FAILED: {e}")
            raise

    async def _send_speed_command(self, speed: int, port: int = 0x00):
        """
        Send a direct mode motor power command to Port A (default).
        speed: -100..100 (we clamp to this range).
        """
        # Clamp speed
        if speed > 100:
            speed = 100
        if speed < -100:
            speed = -100

        # LEGO encodes power as signed byte (-100..100)
        power = speed & 0xFF

        # Port Output Command, WriteDirectModeData, motor mode 0x00
        # [len, hub_id, 0x81, port_id, startup/completion, subcmd(0x51), mode(0x00), power]
        cmd = bytearray([0x08, 0x00, 0x81, port, 0x11, 0x51, 0x00, power])
        await self._send_command_with_logging(cmd, f"Speed={speed}")

    async def _send_led_command(self, color_code: int):
        """
        Set the hub's LED color.
        """
        # Port 0x32 is the LED, mode 0x00, color code 0x00..0x0A
        cmd = bytearray([0x08, 0x00, 0x81, 0x32, 0x11, 0x51, 0x00, color_code & 0xFF])
        color_name = [k for k, v in LED_COLORS.items() if v == color_code]
        desc = f"LED={color_name[0] if color_name else color_code}"
        await self._send_command_with_logging(cmd, desc)

    # ----------------------------
    # Public API
    # ----------------------------
    async def set_speed(self, speed: int):
        """
        Set desired speed; heartbeat loop will keep re-sending it.
        Negative = reverse, positive = forward, 0 = stop.
        """
        print(f"\nSetting speed to {speed}")
        self._desired_speed = speed
        self._speed_was_set = True  # Mark that we've explicitly set a speed

        # Also send immediately, so it reacts without waiting for the next heartbeat tick.
        if self.client and self.client.is_connected:
            try:
                await self._send_speed_command(self._desired_speed)
            except Exception as e:
                print("Immediate speed command failed:", repr(e))

    async def stop(self):
        """Convenience wrapper."""
        await self.set_speed(0)

    async def set_led(self, color_name: str):
        """
        Set hub LED by name, e.g. 'green', 'red', 'white'.
        """
        color_code = LED_COLORS.get(color_name.lower())
        if color_code is None:
            print(f"Unknown LED color '{color_name}', ignoring.")
            return
        print(f"\nSetting LED to {color_name}")
        if self.client and self.client.is_connected:
            try:
                await self._send_led_command(color_code)
            except Exception as e:
                print("LED command failed:", repr(e))

    # Example "show" – tweak as you like
    async def run_show(self):
        """
        Simple demo sequence:
        - LED green
        - Accelerate forward
        - Brief cruise
        - Brake to stop
        """
        print("\n" + "="*60)
        print("DEMO SHOW")
        print("="*60)
        
        await self.set_led("green")
        await asyncio.sleep(0.5)  # Let LED command settle

        # ramp up
        for s in range(0, 60, 10):  # 0,10,20,30,40,50
            await self.set_speed(s)
            await asyncio.sleep(0.7)

        # cruise
        print("\nCruising...")
        await asyncio.sleep(3.0)

        # ramp down
        print("\nSlowing down...")
        for s in range(50, -10, -10):  # 50,40,30,20,10,0
            await self.set_speed(s)
            await asyncio.sleep(0.5)

        await self.stop()
        await asyncio.sleep(0.3)
        await self.set_led("white")
        print("\n✓ Demo show complete.")


# ----------------------------
# Diagnostic test sequence
# ----------------------------
async def diagnostic_test():
    """
    Minimal diagnostic test to understand what the hub accepts.
    """
    print("\n" + "="*60)
    print("DIAGNOSTIC TEST MODE")
    print("="*60)
    
    hub = TrainHub()
    ok = await hub.connect()
    if not ok:
        print("\n✗ Connection failed")
        return

    try:
        print("\n--- Test 1: LED Color Changes ---")
        for color in ["green", "blue", "red", "white"]:
            print(f"\nChanging LED to {color}...")
            await hub.set_led(color)
            await asyncio.sleep(1.0)
        
        print("\n--- Test 2: Very Slow Speed Commands ---")
        speeds = [10, 0, -10, 0]
        for speed in speeds:
            print(f"\nSetting speed to {speed}...")
            await hub.set_speed(speed)
            await asyncio.sleep(2.0)  # Long delay between commands
        
        print("\n--- Test 3: LED + Motor Combination ---")
        await hub.set_led("green")
        await asyncio.sleep(0.5)
        await hub.set_speed(20)
        await asyncio.sleep(3.0)
        await hub.set_speed(0)
        await asyncio.sleep(0.5)
        await hub.set_led("white")
        
        print("\n--- Test 4: Idle Connection Test ---")
        print("Keeping connection alive with just heartbeat for 10 seconds...")
        await asyncio.sleep(10)
        
        print("\n✓ All tests complete")
        
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await hub.disconnect()


# ----------------------------
# Standalone test runner
# ----------------------------
async def main():
    # Choose which test to run
    # Option 1: Run diagnostic test
    await diagnostic_test()
    
    # Option 2: Run the demo show (uncomment to use)
    # hub = TrainHub()
    # ok = await hub.connect()
    # if ok:
    #     try:
    #         await hub.run_show()
    #         print("Keeping connection alive for 10 more seconds...")
    #         await asyncio.sleep(10)
    #     finally:
    #         await hub.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
