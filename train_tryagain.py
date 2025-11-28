import asyncio
import time
from bleak import BleakScanner, BleakClient

UART_UUID = "00001624-1212-efde-1623-785feabcd123"
TARGET_NAME = "HUB NO.4"
HARDCODED_ADDRESS = None

LED_COLORS = {
    "off": 0x00, "pink": 0x01, "purple": 0x02, "blue": 0x03,
    "light_blue": 0x04, "cyan": 0x05, "green": 0x06, "yellow": 0x07,
    "orange": 0x08, "white": 0x09, "red": 0x0A,
}


class SimpleTrainHub:
    """
    Simplified approach based on ksimes/trainControl repository.
    Key difference: No aggressive heartbeat, let the hub breathe.
    """

    def __init__(self, address=None):
        self.address = address
        self.client = None
        self._ports_ready = asyncio.Event()
        self._ports_discovered = set()
        self._connection_start = 0

    @staticmethod
    async def discover_address():
        print("Scanning for HUB NO.4...")
        devices = await BleakScanner.discover(timeout=6.0)
        for d in devices:
            if TARGET_NAME in (d.name or ""):
                print(f"Found hub: {d.name} [{d.address}]")
                return d.address
        print("No hub found.")
        return None

    def _log(self, direction, data, description=""):
        """Log messages with timestamp."""
        elapsed = time.time() - self._connection_start
        hex_str = " ".join(f"{b:02X}" for b in data)
        print(f"[{elapsed:6.3f}s] {direction}: {hex_str} {description}")

    def _notification_handler(self, _char, data: bytearray):
        """Handle notifications from the hub."""
        self._log("RX", data)
        
        if len(data) >= 3 and data[2] == 0x04:  # Port attachment
            port_id = data[3]
            event = data[4]
            
            if event == 0x01:  # Attached
                self._ports_discovered.add(port_id)
                io_type = data[5] if len(data) > 5 else 0
                type_names = {0x02: "Motor", 0x17: "LED", 0x14: "Voltage", 0x15: "Current"}
                print(f"  → Port 0x{port_id:02X}: {type_names.get(io_type, 'Unknown')}")
                
                if len(self._ports_discovered) >= 4:
                    print("  → All ports ready!")
                    self._ports_ready.set()

    async def connect(self):
        """Connect and wait for hub to be ready."""
        if not self.address:
            self.address = HARDCODED_ADDRESS or await self.discover_address()
            if not self.address:
                return False

        print(f"\nConnecting to {self.address}...")
        self.client = BleakClient(self.address)
        
        try:
            await self.client.connect(timeout=20.0)
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

        if not self.client.is_connected:
            print("Connection failed (not connected)")
            return False

        self._connection_start = time.time()
        print("✓ Connected")
        
        # Subscribe to notifications
        await self.client.start_notify(UART_UUID, self._notification_handler)
        print("✓ Notifications enabled")
        
        # Wait for port discovery (passively - don't send commands!)
        print("\nWaiting for port discovery...")
        try:
            await asyncio.wait_for(self._ports_ready.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            print(f"✗ Timeout (only {len(self._ports_discovered)} ports found)")
            await self.disconnect()
            return False

        print("✓ Hub ready\n")
        return True

    async def disconnect(self):
        """Disconnect from hub."""
        if self.client and self.client.is_connected:
            try:
                await self.client.stop_notify(UART_UUID)
                await self.client.disconnect()
            except Exception:
                pass
        print("Disconnected")

    async def _send_command(self, cmd: bytearray, description: str):
        """Send a command to the hub."""
        if not self.client or not self.client.is_connected:
            print(f"✗ Cannot send {description}: Not connected")
            return False
        
        self._log("TX", cmd, f"({description})")
        try:
            await self.client.write_gatt_char(UART_UUID, cmd)
            print(f"  ✓ {description} sent")
            return True
        except Exception as e:
            print(f"  ✗ {description} failed: {e}")
            return False

    async def set_speed(self, speed: int, port: int = 0x00):
        """
        Set motor speed.
        Key difference: Use 0x01 (execute immediately, no feedback request)
        instead of 0x11 which might be causing disconnects.
        """
        speed = max(-100, min(100, speed))
        power = speed & 0xFF
        
        # Use 0x01 instead of 0x11 - don't request feedback!
        cmd = bytearray([0x08, 0x00, 0x81, port, 0x01, 0x51, 0x00, power])
        return await self._send_command(cmd, f"Speed={speed}")

    async def set_led(self, color_name: str):
        """Set LED color."""
        color_code = LED_COLORS.get(color_name.lower(), 0x09)
        
        # Also use 0x01 for LED commands
        cmd = bytearray([0x08, 0x00, 0x81, 0x32, 0x01, 0x51, 0x00, color_code])
        return await self._send_command(cmd, f"LED={color_name}")


async def test_simple():
    """Test the simplified approach."""
    print("="*60)
    print("SIMPLIFIED TEST (no aggressive heartbeat)")
    print("="*60)
    
    hub = SimpleTrainHub()
    if not await hub.connect():
        return

    try:
        # Test 1: LED only
        print("\n--- Test 1: LED Changes ---")
        for color in ["green", "blue", "red", "white"]:
            await hub.set_led(color)
            await asyncio.sleep(2.0)  # Long pause between commands
        
        # Test 2: Motor commands
        print("\n--- Test 2: Motor Commands ---")
        await hub.set_led("green")
        await asyncio.sleep(1.0)
        
        await hub.set_speed(20)
        await asyncio.sleep(3.0)
        
        await hub.set_speed(0)
        await asyncio.sleep(1.0)
        
        # Test 3: Just wait
        print("\n--- Test 3: Idle Wait (no commands) ---")
        await asyncio.sleep(5.0)
        
        await hub.set_led("white")
        print("\n✓ Tests complete")
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await hub.disconnect()


if __name__ == "__main__":
    asyncio.run(test_simple())