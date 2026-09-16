"""WiX 4 installer authoring for the unpacked Windows application."""
import hashlib
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid
import xml.etree.ElementTree as ET

# This identifies the installed product family. NEVER change it between releases.
UPGRADE_CODE = "80CEB237-EBBD-4B9A-A183-37D32811B08F"
WIX_VERSION = "4.0.6"
NS = "http://wixtoolset.org/schemas/v4/wxs"
UI_NS = NS + "/ui"


def validate_version(version):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("MSI versions must be major.minor.build, for example 0.1.1 (no suffix or fourth field).")
    if any(n > limit for n, limit in zip(map(int, version.split('.')), (255, 255, 65535))):
        raise ValueError("MSI version limits are 255.255.65535.")


def tool_problem():
    if not shutil.which("wix"):
        return "MSI needs WiX 4.0.6 and its UI extension; see the Windows MSI setup instructions"
    try:
        version = subprocess.check_output(["wix", "--version"], text=True, stderr=subprocess.STDOUT).strip()
        if not version.startswith(WIX_VERSION + "+") and version != WIX_VERSION:
            return f"MSI expects WiX {WIX_VERSION}; found {version}"
        extensions = subprocess.check_output(["wix", "extension", "list", "--global"], text=True, stderr=subprocess.STDOUT)
        if "WixToolset.UI.wixext" not in extensions or WIX_VERSION not in extensions:
            return f"run: wix extension add --global WixToolset.UI.wixext/{WIX_VERSION}"
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"WiX could not run: {exc}"
    return None


def executable_arch(path):
    with path.open("rb") as exe:
        if exe.read(2) != b"MZ":
            raise ValueError("MSI requires a native Windows executable.")
        exe.seek(0x3C)
        offset = struct.unpack("<I", exe.read(4))[0]
        exe.seek(offset)
        if exe.read(4) != b"PE\0\0":
            raise ValueError("Invalid Windows executable header.")
        machine = struct.unpack("<H", exe.read(2))[0]
    try:
        return {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64"}[machine]
    except KeyError:
        raise ValueError(f"Unsupported Windows architecture: {machine:#x}") from None


def author_installer(bundle, work, version, arch, license_path):
    validate_version(version)
    ET.register_namespace("", NS)
    ET.register_namespace("ui", UI_NS)

    def node(parent, tag, **attrs):
        return ET.SubElement(parent, f"{{{NS}}}{tag}", attrs)

    def identifier(prefix, path):
        return prefix + hashlib.sha256(path.encode()).hexdigest()[:24]

    wix = ET.Element(f"{{{NS}}}Wix")
    package = node(wix, "Package", Name="PowerTerm", Manufacturer="PowerTerm",
                   Version=version, UpgradeCode=UPGRADE_CODE, Scope="perMachine",
                   Compressed="yes", InstallerVersion="500", Language="1033")
    node(package, "MajorUpgrade", DowngradeErrorMessage="A newer version of PowerTerm is already installed.",
         AllowSameVersionUpgrades="yes", Schedule="afterInstallInitialize")
    node(package, "MediaTemplate", EmbedCab="yes", CompressionLevel="high")
    node(package, "Property", Id="ARPNOMODIFY", Value="1")
    program_files = node(package, "StandardDirectory", Id="ProgramFiles6432Folder")
    install = node(program_files, "Directory", Id="INSTALLFOLDER", Name="PowerTerm")
    node(package, "StandardDirectory", Id="ProgramMenuFolder")
    feature = node(package, "Feature", Id="Main", Title="PowerTerm", Level="1")
    directories = {".": install}
    files = sorted(path for path in bundle.rglob("*") if path.is_file())
    if not (bundle / "PowerTerm.exe").is_file():
        raise ValueError("Missing PowerTerm.exe in the directory bundle.")
    for path in files:
        relative = path.relative_to(bundle)
        if path.name.lower() in ("config.json", "hosts.json"):
            raise ValueError("User configuration must never be included in the MSI payload.")
        parent = Path(".")
        for part in relative.parts[:-1]:
            current = parent / part
            key = current.as_posix()
            if key not in directories:
                directories[key] = node(directories[parent.as_posix()], "Directory", Id=identifier("D", key), Name=part)
            parent = current
        key = relative.as_posix()
        component_id = identifier("C", key)
        component = node(directories[parent.as_posix()], "Component", Id=component_id,
                         Guid=str(uuid.uuid5(uuid.UUID(UPGRADE_CODE), arch + ":" + key)).upper())
        file_node = node(component, "File", Id=identifier("F", key), Source=str(path.resolve()), KeyPath="yes")
        if key == "PowerTerm.exe":
            node(file_node, "Shortcut", Id="StartMenuShortcut", Directory="ProgramMenuFolder",
                 Name="PowerTerm", Advertise="yes", WorkingDirectory="INSTALLFOLDER")
        node(feature, "ComponentRef", Id=component_id)
    # Render the unchanged GPL text for WiX's standard installer dialog.
    text = license_path.read_text(encoding="utf-8")
    escaped = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\par\n")
    rtf = work / "License.rtf"
    rtf.write_text("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Courier New;}}\\f0\\fs16\n" + escaped + "}", encoding="ascii")
    node(package, "WixVariable", Id="WixUILicenseRtf", Value=str(rtf.resolve()))
    ET.SubElement(package, f"{{{UI_NS}}}WixUI", {"Id": "WixUI_Minimal"})
    source = work / "PowerTerm.wxs"
    ET.indent(wix)
    ET.ElementTree(wix).write(source, encoding="utf-8", xml_declaration=True)
    return source


def build_msi(bundle, work, version, license_path):
    problem = tool_problem()
    if problem:
        raise ValueError(problem)
    arch = executable_arch(bundle / "PowerTerm.exe")
    source = author_installer(bundle, work, version, arch, license_path)
    output = work / f"PowerTerm-{version}-{arch}.msi"
    subprocess.run(["wix", "build", str(source), "-arch", arch, "-ext", "WixToolset.UI.wixext",
                    "-o", str(output)], check=True)
    return output
