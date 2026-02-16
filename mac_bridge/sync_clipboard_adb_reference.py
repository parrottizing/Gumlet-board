import time
import subprocess
import shlex
import pyperclip
import sys
import os
import threading
import queue
import base64
import tempfile
from PIL import ImageGrab, Image, ImageOps
from io import BytesIO
import urllib.parse

# Global queue to communicate between threads
clipboard_event_queue = queue.Queue()

def get_connected_devices():
    """Returns a list of unique connected device IDs."""
    try:
        output = subprocess.check_output(["adb", "devices"]).decode("utf-8")
        raw_devices = []
        for line in output.splitlines()[1:]:
            if line.strip() and "device" in line:
                parts = line.split()
                if parts[1] == "device":
                    raw_devices.append(parts[0])

        unique_devices = []
        seen_serials = set()

        for device_id in raw_devices:
            try:
                # Get real serial number to handle duplicates (e.g. IP vs mDNS)
                serial = subprocess.check_output(
                    ["adb", "-s", device_id, "shell", "getprop", "ro.serialno"],
                    timeout=5
                ).decode("utf-8").strip()

                if serial and serial not in seen_serials:
                    seen_serials.add(serial)
                    unique_devices.append(device_id)
                elif serial in seen_serials:
                    # print(f"Skipping duplicate device handle {device_id} for serial {serial}")
                    pass
            except Exception as e:
                print(f"Warning: Could not get serial for {device_id}: {e}")
                # If we can't get serial, keep it to be safe
                unique_devices.append(device_id)
        
        return unique_devices
    except subprocess.CalledProcessError:
        return []

def send_text_to_device(device_id, text):
    """Sends text to a specific Android device via ADB broadcast using stdin."""
    quoted_text = shlex.quote(text)
    cmd_str = f"am broadcast -a com.example.clipboard.WRITE -n com.example.clipboard/.WriteReceiver -e text {quoted_text}"
    
    try:
        process = subprocess.Popen(
            ["adb", "-s", device_id, "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate(input=cmd_str)
        
        if process.returncode == 0:
            if "Error" in stderr or "inaccessible" in stderr:
                 print(f"[{device_id}] Potential error sending text: {stderr.strip()}")
            else:
                 print(f"[{device_id}] Sent text to Android: {text[:30]}..." if len(text) > 30 else f"[{device_id}] Sent text to Android: {text}")
        else:
            print(f"[{device_id}] ADB failed sending text: {stderr.strip()}")
            
    except Exception as e:
        print(f"[{device_id}] Exception during text send: {e}")

def send_image_to_device(device_id, image):
    """Sends image to a specific Android device via ADB broadcast."""
    try:
        # Convert image to PNG bytes
        img_byte_arr = BytesIO()
        image.save(img_byte_arr, format='PNG')
        img_bytes = img_byte_arr.getvalue()
        
        # Check size limit (50MB)
        if len(img_bytes) > 50 * 1024 * 1024:
            print(f"[{device_id}] Image too large ({len(img_bytes)} bytes), skipping")
            return
        
        # Encode to Base64
        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        
        # Create a temp file with image data
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as temp_file:
            temp_file.write(f"image/png\n")
            temp_file.write(f"clipboard_image.png\n")
            temp_file.write(base64_image)
            temp_path = temp_file.name
        
        try:
            # Push the file to device in a location WriteReceiver can read (app-specific storage)
            push_path = "/sdcard/Android/data/com.example.clipboard/files/clipboard_image_from_mac.txt"
            subprocess.run(
                ["adb", "-s", device_id, "push", temp_path, push_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True
            )
            
            # Broadcast with just the file path - WriteReceiver will read the file
            cmd = f'am broadcast -a com.example.clipboard.WRITE -n com.example.clipboard/.WriteReceiver -e image_file "{push_path}"'
            
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                print(f"[{device_id}] Sent image to Android ({len(img_bytes)} bytes)")
            else:
                print(f"[{device_id}] Failed to send image broadcast: {result.stderr}")
                
        finally:
            os.unlink(temp_path)
            
    except subprocess.TimeoutExpired:
        print(f"[{device_id}] Timeout sending image")
    except Exception as e:
        print(f"[{device_id}] Exception during image send: {e}")
    except Exception as e:
        print(f"[{device_id}] Exception during image send: {e}")

def read_from_device(device_id):
    """Reads clipboard content (text or image) from a specific Android device."""
    try:
        # Step 0: Clean up old files to avoid stale data
        subprocess.run(["adb", "-s", device_id, "shell", "rm", "/sdcard/Android/data/com.example.clipboard/files/clipboard_content.txt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["adb", "-s", device_id, "shell", "rm", "/sdcard/Android/data/com.example.clipboard/files/clipboard_image_meta.txt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["adb", "-s", device_id, "shell", "rm", "/sdcard/Android/data/com.example.clipboard/files/clipboard_image.bin"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Step 1: Trigger the app to write clipboard to file
        trigger_cmd = ["adb", "-s", device_id, "shell", "am", "start", "-n", "com.example.clipboard/.MainActivity"]
        subprocess.run(trigger_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        
        # Step 2: Poll for files (wait up to 5 seconds)
        start_time = time.time()
        while time.time() - start_time < 5:
            # Check for image metadata
            read_meta_cmd = ["adb", "-s", device_id, "shell", "cat", "/sdcard/Android/data/com.example.clipboard/files/clipboard_image_meta.txt"]
            result_meta = subprocess.run(read_meta_cmd, capture_output=True, text=True, check=False)
            
            if result_meta.returncode == 0 and result_meta.stdout.strip():
                # Found image metadata!
                lines = result_meta.stdout.strip().split('\n', 1)
                if len(lines) >= 1:
                    mime_type = lines[0]
                    filename = lines[1] if len(lines) > 1 else "image"
                    
                    # Pull the image file
                    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                        temp_path = temp_file.name
                    
                    pull_cmd = ["adb", "-s", device_id, "pull", "/sdcard/Android/data/com.example.clipboard/files/clipboard_image.bin", temp_path]
                    subprocess.run(pull_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
                    
                    # Read image data from temp file
                    with open(temp_path, "rb") as f:
                        image_data = f.read()
                    
                    # Clean up temp file
                    os.unlink(temp_path)
                    
                    return {'type': 'image', 'mime_type': mime_type, 'filename': filename, 'data': image_data}
            
            # Check for text
            read_txt_cmd = ["adb", "-s", device_id, "shell", "cat", "/sdcard/Android/data/com.example.clipboard/files/clipboard_content.txt"]
            result_txt = subprocess.run(read_txt_cmd, capture_output=True, text=True, check=False)
            
            if result_txt.returncode == 0 and result_txt.stdout.strip():
                return {'type': 'text', 'data': result_txt.stdout}
            
            # Wait a bit before retrying
            time.sleep(0.2)
        
        print(f"[{device_id}] Timed out waiting for clipboard data")
        return None
        
    except Exception as e:
        print(f"[{device_id}] Exception during read: {e}")
        return None

def set_mac_clipboard_image(image):
    """Sets an image to the Mac clipboard using osascript."""
    try:
        # Save image to temporary file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
            image.save(temp_file, format='PNG')
            temp_path = temp_file.name
        
        try:
            # Use osascript to set clipboard
            script = f'set the clipboard to (read (POSIX file "{temp_path}") as «class PNGf»)'
            subprocess.run(
                ["osascript", "-e", script],
                check=True,
                capture_output=True
            )
            print(f"Image set to Mac clipboard")
            return True
        finally:
            # Clean up temp file after a delay (osascript needs time to read it)
            time.sleep(0.5)
            try:
                os.unlink(temp_path)
            except:
                pass
                
    except Exception as e:
        print(f"Error setting Mac clipboard image: {e}")
        return False

def get_finder_selection():
    """Gets the path of the currently selected file in Finder using AppleScript."""
    try:
        cmd = """
        tell application "Finder"
            if selection is not {} then
                set sel to selection
                set p to POSIX path of (item 1 of sel as alias)
                return p
            end if
        end tell
        """
        result = subprocess.run(["osascript", "-e", cmd], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None

def get_mac_clipboard_image():
    """Gets an image from the Mac clipboard using PIL or Finder selection."""
    try:
        content = ImageGrab.grabclipboard()
        text_content = ""
        try:
            text_content = pyperclip.paste().strip()
        except:
            pass
            
        # Check if we have a filename in text, which suggests a Finder copy
        if text_content and any(text_content.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']):
            # Try to get the actual file from Finder selection
            finder_path = get_finder_selection()
            if finder_path:
                # Check if the selected file matches the text (either full path or filename)
                if finder_path == text_content or os.path.basename(finder_path) == text_content:
                    if os.path.isfile(finder_path):
                        try:
                            image = Image.open(finder_path)
                            image.load()
                            return image
                        except:
                            pass

        # Case 1: Direct image data (e.g. copied from browser or image editor)
        if isinstance(content, Image.Image):
            return content
            
        # Case 2: File paths (e.g. copied from Finder, if ImageGrab supports it)
        elif isinstance(content, list) and content:
            # Check if the first item is a valid image file
            file_path = content[0]
            if os.path.isfile(file_path):
                try:
                    image = Image.open(file_path)
                    # Force load the image data so we can close the file
                    image.load()
                    return image
                except Exception:
                    # Not an image file or cannot open
                    pass

        # Case 3: Check for file:// URL in text content (e.g. Telegram)
        # Try text from pyperclip first
        potential_urls = []
        if text_content:
            potential_urls.append(text_content)
        
        # Also try to get clipboard as text via osascript if pyperclip failed or just to be safe
        try:
            cmd = 'osascript -e "get the clipboard as text"'
            result = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                potential_urls.append(result.stdout.strip())
        except:
            pass

        for url_text in potential_urls:
            if url_text.strip().startswith('file://'):
                try:
                    parsed = urllib.parse.urlparse(url_text.strip())
                    file_path = urllib.parse.unquote(parsed.path)
                    if os.path.isfile(file_path):
                        # Check extension
                        if any(file_path.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']):
                            try:
                                image = Image.open(file_path)
                                image.load()
                                return image
                            except:
                                pass
                except:
                    pass

        # Case 4: Check for '«class furl»' (File URL) directly via AppleScript
        # This is common for apps like Telegram that put a file reference on the clipboard
        try:
            # Get the POSIX path of the file URL directly
            cmd = 'osascript -e "get POSIX path of (the clipboard as «class furl»)"'
            result = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                furl_path = result.stdout.strip()
                if os.path.isfile(furl_path):
                     # Check extension
                    if any(furl_path.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']):
                        try:
                            image = Image.open(furl_path)
                            image.load()
                            return image
                        except:
                            pass
        except:
            pass

        return None
    except Exception as e:
        # ImageGrab might not be available on all systems
        print(f"Error getting Mac clipboard image: {e}")
        return None

class LogcatMonitor(threading.Thread):
    def __init__(self, device_id):
        super().__init__()
        self.device_id = device_id
        self.daemon = True
        self.stop_event = threading.Event()

    def run(self):
        print(f"[{self.device_id}] Starting Logcat Monitor...")
        # Clear logs first
        subprocess.run(["adb", "-s", self.device_id, "logcat", "-c"], stderr=subprocess.DEVNULL)
        
        process = subprocess.Popen(
            ["adb", "-s", self.device_id, "logcat", "-v", "time"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors='replace'
        )
        
        try:
            while not self.stop_event.is_set():
                line = process.stdout.readline()
                if not line:
                    break
                
                # Check for specific keywords indicating a copy action
                if "SemClipboardToastController" in line and "Copy toast is shown" in line:
                    clipboard_event_queue.put(self.device_id)
                elif "ClipboardListener" in line and "showCopyToast" in line:
                    clipboard_event_queue.put(self.device_id)
                # Also listen for our own app's logs to confirm it saw the change
                elif "ClipboardMonitor" in line and "Clipboard changed:" in line:
                    clipboard_event_queue.put(self.device_id)
                    
        except Exception as e:
            print(f"[{self.device_id}] Logcat monitor error: {e}")
        finally:
            process.terminate()

def compute_image_hash(image):
    """Compute a simple hash of an image for comparison."""
    if image is None:
        return None
    try:
        # Convert to bytes and hash
        img_byte_arr = BytesIO()
        # Resize to small size for quick comparison
        thumb = image.copy()
        thumb.thumbnail((64, 64))
        thumb.save(img_byte_arr, format='PNG')
        return hash(img_byte_arr.getvalue())
    except:
        return None

def main():
    print("Two-way Clipboard Sync Started (Mac <-> Android)...")
    print("Text and Image support enabled.")
    print("Ensure 'Clipboard Sync' app is installed on Android device.")
    
    # Install Pillow if not available
    try:
        import PIL
    except ImportError:
        print("Installing Pillow...")
        subprocess.run([sys.executable, "-m", "pip", "install", "Pillow"], check=True)
        print("Pillow installed. Please restart the script.")
        return
    
    last_mac_text = ""
    last_android_clipboard = ""
    last_mac_image_hash = None
    last_android_image_hash = None
    
    # Initialize last_mac_text
    try:
        last_mac_text = pyperclip.paste()
    except:
        pass
    
    # Initialize last_mac_image_hash
    last_mac_image_hash = compute_image_hash(get_mac_clipboard_image())

    # Start logcat monitors for connected devices
    devices = get_connected_devices()
    monitors = {}
    for device in devices:
        monitor = LogcatMonitor(device)
        monitor.start()
        monitors[device] = monitor

    # Track last send time to avoid feedback loops
    last_send_time = {}
    # Track last read time globally to debounce rapid events across duplicate device entries
    last_global_read_time = 0

    try:
        while True:
            # Check for new devices or disconnected devices (basic handling)
            current_devices = get_connected_devices()
            for device in current_devices:
                if device not in monitors:
                    monitor = LogcatMonitor(device)
                    monitor.start()
                    monitors[device] = monitor
            
            # --- Mac to Android ---
            # --- Mac to Android ---
            
            # Check for image changes FIRST
            current_mac_image = get_mac_clipboard_image()
            current_mac_image_hash = compute_image_hash(current_mac_image)
            image_sent = False
            
            if current_mac_image_hash is not None and current_mac_image_hash != last_mac_image_hash:
                # New image in clipboard
                for device in current_devices:
                    send_image_to_device(device, current_mac_image)
                    last_send_time[device] = time.time()
                last_mac_image_hash = current_mac_image_hash
                image_sent = True

            # Check for text changes
            try:
                current_mac_text = pyperclip.paste()
            except Exception as e:
                current_mac_text = last_mac_text

            if current_mac_text != last_mac_text:
                should_send_text = True
                
                # If we just sent an image, check if the text is likely the filename
                if image_sent:
                    if any(current_mac_text.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp']):
                         should_send_text = False
                         print(f"Skipping text send because it looks like the filename of the image just sent: {current_mac_text}")

                if should_send_text and current_mac_text.strip():
                    for device in current_devices:
                        send_text_to_device(device, current_mac_text)
                        last_send_time[device] = time.time()
                    last_android_clipboard = current_mac_text
                
                # Always update last_mac_text so we don't send it next time
                last_mac_text = current_mac_text

            # --- Android to Mac (Event Driven) ---
            try:
                # Check if any device reported a copy event
                while True:
                    event_device_id = clipboard_event_queue.get_nowait()
                    
                    current_time = time.time()
                    
                    # Check for feedback loop (ignore events shortly after we sent data)
                    if event_device_id in last_send_time:
                        if current_time - last_send_time[event_device_id] < 3.0:
                            print(f"[{event_device_id}] Ignoring echo event (feedback loop protection)")
                            continue

                    # Check for rapid duplicate events (global debounce)
                    if current_time - last_global_read_time < 1.0:
                         continue

                    print(f"[{event_device_id}] Detected copy event! Syncing...")
                    
                    clipboard_data = read_from_device(event_device_id)
                    
                    if clipboard_data is not None:
                        if clipboard_data['type'] == 'text':
                            text_data = clipboard_data['data']
                            if text_data != last_android_clipboard and text_data != current_mac_text:
                                if text_data.strip():
                                    print(f"[{event_device_id}] Received text from Android: {text_data[:30]}..." if len(text_data) > 30 else f"[{event_device_id}] Received text from Android: {text_data}")
                                    pyperclip.copy(text_data)
                                    last_mac_text = text_data
                                    last_android_clipboard = text_data
                                    last_global_read_time = current_time
                        
                        elif clipboard_data['type'] == 'image':
                            try:
                                # Load image from raw bytes
                                image_bytes = clipboard_data['data']
                                image = Image.open(BytesIO(image_bytes))
                                
                                # Apply EXIF orientation
                                try:
                                    image = ImageOps.exif_transpose(image)
                                except Exception as e:
                                    print(f"[{event_device_id}] Warning: Could not apply EXIF orientation: {e}")
                                
                                # Check for duplicate image from Android
                                current_image_hash = compute_image_hash(image)
                                if current_image_hash == last_android_image_hash:
                                    print(f"[{event_device_id}] Ignoring duplicate image event from Android")
                                    continue

                                # Set to Mac clipboard
                                if set_mac_clipboard_image(image):
                                    print(f"[{event_device_id}] Received image from Android: {clipboard_data['filename']} ({len(image_bytes)} bytes)")
                                    last_mac_image_hash = current_image_hash
                                    last_android_image_hash = current_image_hash
                                    last_global_read_time = current_time
                            except Exception as e:
                                print(f"[{event_device_id}] Error processing image: {e}")
            
            except queue.Empty:
                pass
            
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        print("\nStopping clipboard sync.")
        for monitor in monitors.values():
            monitor.stop_event.set()

if __name__ == "__main__":
    main()
