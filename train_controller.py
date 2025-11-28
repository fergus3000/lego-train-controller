import asyncio
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

    Features:
    - Connect / disconnect
    - Subscribe to notifications
    - Background heartbeat to keep the connection alive
    - set_speed(), stop(), set_led(), run_show()
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

    # ----------------------------
    # Discovery / connection
    # ----------------------------
    @staticmethod
    async def discover_address():
        """Find the first HUB NO.4 in range and return its address."""
        print("Scanning for HUB NO.4...")
        devices = await BleakScanner.discover(timeout=20.0)
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

        print(f"Connecting to {self.address} ...")
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

        print("Connected. Waiting for hub to initialize...")

        # Subscribe to notifications (like the real remote does)
        await self.client.start_notify(UART_UUID, self._notification_handler)

        # IMPORTANT: Give the hub time to discover its ports and initialize
        # The hub sends several 0x04 (port attachment) messages during this time
        # Wait longer to ensure all ports are registered
        print("Waiting 4 seconds for port discovery...")
        await asyncio.sleep(4.0)

        # Mark as initialized
        self._initialized = True
        
        # Optional: set an initial LED color so you can see it's under Pi control
        print("Setting initial LED color...")
        await self.set_led("green")
        
        # Give LED command time to process
        await asyncio.sleep(1.0)
        
        # NOW start heartbeat loop - after all initialization is complete
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        print("Hub ready for commands.")
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
        We're not decoding everything here, but you can log/inspect.
        """
        # For now, just print a short hex preview for debugging.
        # Disable this later if it's too noisy.
        hex_preview = " ".join(f"{b:02X}" for b in data[:8])
        print(f"[notify] {hex_preview} ...")

    # ----------------------------
    # Heartbeat
    # ----------------------------
    async def _heartbeat_loop(self):
        """
        Periodically re-send the current speed command OR send a keep-alive.
        This keeps traffic flowing (like the official remote),
        which helps prevent the hub from dropping the connection.
        """
        interval = 0.15  # 150 ms, similar to the real remote's chatter

        print("Heartbeat loop started.")
        while self._running and self.client and self.client.is_connected:
            try:
                # Only send speed commands if initialized and we have a non-zero speed
                if self._initialized and self._desired_speed != 0:
                    await self._send_speed_command(self._desired_speed)
                elif self._initialized:
                    # Send a simple "hub properties" request as keep-alive
                    # This is a lightweight command that keeps the connection active
                    await self._send_keep_alive()
            except Exception as e:
                print("Heartbeat write failed:", repr(e))
                # If we fail here, likely the hub disconnected.
                self._running = False
                break

            await asyncio.sleep(interval)

        print("Heartbeat loop exiting (client disconnected or stopped).")

    # ----------------------------
    # Low-level commands
    # ----------------------------
    async def _send_keep_alive(self):
        """
        Send a lightweight keep-alive message to maintain the connection.
        This is a Hub Properties request for the hub name.
        """
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Not connected")
        
        # Hub Properties: Get Advertising Name (property 0x01)
        # [len, hub_id, msg_type(0x01), property(0x01)]
        cmd = bytearray([0x05, 0x00, 0x01, 0x01, 0x05])
        await self.client.write_gatt_char(UART_UUID, cmd)

    async def _send_speed_command(self, speed: int, port: int = 0x00):
        """
        Send a direct mode motor power command to Port A (default).
        speed: -100..100 (we clamp to this range).
        """
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Not connected")

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
        await self.client.write_gatt_char(UART_UUID, cmd)

    async def _send_led_command(self, color_code: int):
        """
        Set the hub's LED color.
        """
        if not self.client or not self.client.is_connected:
            raise RuntimeError("Not connected")

        # Port 0x32 is the LED, mode 0x00, color code 0x00..0x0A
        cmd = bytearray([0x08, 0x00, 0x81, 0x32, 0x11, 0x51, 0x00, color_code & 0xFF])
        await self.client.write_gatt_char(UART_UUID, cmd)

    # ----------------------------
    # Public API
    # ----------------------------
    async def set_speed(self, speed: int):
        """
        Set desired speed; heartbeat loop will keep re-sending it.
        Negative = reverse, positive = forward, 0 = stop.
        """
        print(f"Setting speed to {speed}")
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
        print(f"Setting LED to {color_name}")
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
        print("Starting demo show...")
        await self.set_led("green")
        await asyncio.sleep(0.5)  # Let LED command settle

        # ramp up
        for s in range(0, 60, 10):  # 0,10,20,30,40,50
            await self.set_speed(s)
            await asyncio.sleep(0.7)

        # cruise
        await asyncio.sleep(3.0)

        # ramp down
        for s in range(50, -10, -10):  # 50,40,30,20,10,0
            await self.set_speed(s)
            await asyncio.sleep(0.5)

        await self.stop()
        await asyncio.sleep(0.3)
        await self.set_led("white")
        print("Demo show complete.")
        

# ----------------------------
# Standalone test runner
# ----------------------------
async def main():
    hub = TrainHub()
    ok = await hub.connect()
    if not ok:
        return

    try:
        # Run a simple show once, then keep the connection alive for a bit.
        await hub.run_show()
        print("Keeping connection alive for 10 more seconds...")
        await asyncio.sleep(10)
    finally:
        await hub.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
