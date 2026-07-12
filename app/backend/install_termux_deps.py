import os
import sys
import platform
import urllib.request
import subprocess

def main():
    print(">>> TradeSignal Termux Dependency Helper <<<")
    
    # 1. Detect Python Version
    py_major = sys.version_info.major
    py_minor = sys.version_info.minor
    py_tag = f"cp{py_major}{py_minor}"
    
    # 2. Detect target download tag (what actually exists in Eutalix Releases)
    arch = platform.machine()
    is_64bit = sys.maxsize > 2**32
    
    if (arch == "aarch64" or arch == "arm64") and is_64bit:
        dl_plat_tag = "linux_aarch64"
    elif (arch == "aarch64" or arch == "arm64") and not is_64bit:
        dl_plat_tag = "linux_armv7l"
    elif arch in ("armv7l", "armv8l"):
        dl_plat_tag = "linux_armv7l"
    elif arch == "x86_64":
        dl_plat_tag = "linux_x86_64"
    elif arch in ("i686", "i386"):
        dl_plat_tag = "linux_i686"
    else:
        print(f"❌ Unsupported architecture for precompiled wheels: {arch}")
        sys.exit(1)
        
    print(f"   - Python version: {py_major}.{py_minor} ({py_tag})")
    print(f"   - Platform target (download): {dl_plat_tag} (64bit={is_64bit})")
    
    whl_name = f"pydantic_core-2.46.3-{py_tag}-{py_tag}-{dl_plat_tag}.whl"
    url = f"https://github.com/Eutalix/android-pydantic-core/releases/download/v2.46.3/{whl_name}"
    
    print(f"   - Downloading precompiled wheel: {whl_name}...")
    try:
        import ssl
        context = ssl._create_unverified_context()
        # Add custom User-Agent to bypass GitHub's default Python-urllib blocks
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        )
        with urllib.request.urlopen(req, context=context) as response:
            with open(whl_name, 'wb') as out_file:
                out_file.write(response.read())
        print("   - Download complete.")
    except Exception as e:
        print(f"⚠️  Download failed: {e}")
        whl_name = None
        
    installed_core = False
    
    if whl_name and os.path.exists(whl_name):
        print("   - Installing wheel locally...")
        # Try direct installation
        proc = subprocess.run([sys.executable, "-m", "pip", "install", f"./{whl_name}"], capture_output=True, text=True)
        if proc.returncode == 0:
            print("   - Successfully installed pydantic-core wheel ✓")
            installed_core = True
            os.remove(whl_name)
        else:
            print(f"   - Direct installation failed (exit {proc.returncode}). Output:")
            print(proc.stderr or proc.stdout)
            print("   - Translating tag...")
            try:
                # Detect the preferred tag components that pip expects on this system
                try:
                    from packaging.tags import sys_tags
                    pref_tag = list(sys_tags())[0]
                    pref_interp = pref_tag.interpreter
                    pref_abi = pref_tag.abi
                    pref_plat = pref_tag.platform
                except Exception:
                    # Fallback to pip internal
                    from pip._internal.utils.compatibility_tags import get_supported
                    pref_tag = get_supported()[0]
                    pref_interp = pref_tag.interpreter
                    pref_abi = pref_tag.abi
                    pref_plat = pref_tag.platform
                
                # Rename the wheel to match the preferred local platform tag completely
                new_whl_name = f"pydantic_core-2.46.3-{pref_interp}-{pref_abi}-{pref_plat}.whl"
                print(f"   - Renaming: {whl_name} -> {new_whl_name}")
                os.rename(whl_name, new_whl_name)
                
                proc = subprocess.run([sys.executable, "-m", "pip", "install", f"./{new_whl_name}"], capture_output=True, text=True)
                if proc.returncode == 0:
                    print("   - Successfully installed pydantic-core wheel with translated tag ✓")
                    installed_core = True
                else:
                    print(f"   - Tag translated installation failed (exit {proc.returncode}). Output:")
                    print(proc.stderr or proc.stdout)
                    
                if os.path.exists(new_whl_name):
                    os.remove(new_whl_name)
            except Exception as e:
                print(f"⚠️  Tag translation failed: {e}")
                if os.path.exists(whl_name):
                    os.remove(whl_name)
                    
        if not installed_core:
            # Output compatible tags list to help debugging
            try:
                from packaging.tags import sys_tags
                print("   - Compatible tags supported by your platform:")
                for tag in list(sys_tags())[:15]:
                    print(f"     - {tag}")
            except Exception:
                try:
                    from pip._internal.utils.compatibility_tags import get_supported
                    print("   - Compatible tags supported by your platform (pip internal):")
                    for tag in get_supported()[:15]:
                        print(f"     - {tag}")
                except Exception:
                    pass
                    
    # 3. Fallback online if local wheel didn't succeed
    if not installed_core:
        print("   - Attempting online install via eutalix community index...")
        proc = subprocess.run([
            sys.executable, "-m", "pip", "install", "pydantic-core", 
            "--extra-index-url", "https://eutalix.github.io/android-pydantic-core/"
        ])
        if proc.returncode == 0:
            print("   - Successfully installed pydantic-core via community index ✓")
            installed_core = True
            
    if not installed_core:
        print("❌ Failed to install pydantic-core. Falling back to default pip (which may require compilation)...")
        
    # 4. Install google-genai
    print("   - Installing google-genai...")
    proc = subprocess.run([sys.executable, "-m", "pip", "install", "google-genai"])
    if proc.returncode == 0:
        print("🚀 Successfully installed google-genai!")
        sys.exit(0)
    else:
        print("❌ Failed to install google-genai.")
        sys.exit(1)

if __name__ == "__main__":
    main()
