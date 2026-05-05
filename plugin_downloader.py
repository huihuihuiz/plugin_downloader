import os
import shutil
import zipfile
import io
import folder_paths
from server import PromptServer
from aiohttp import web

# Max upload size: 500 MB
MAX_UPLOAD_SIZE = 500 * 1024 * 1024

# ---------------------------------------
# Secure path join helper
# ---------------------------------------
def safe_join(base_dir, user_path):
    base = os.path.abspath(base_dir)
    final_path = os.path.abspath(os.path.join(base, user_path))

    # Ensure path is strictly inside base directory
    if os.path.commonpath([base, final_path]) != base:
        raise ValueError("Illegal path")

    return final_path


class PluginDownloader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "plugin_name": ("STRING", {"default": "example_plugin"}),
            }
        }

    RETURN_TYPES = ()
    FUNCTION = "download_plugin"
    OUTPUT_NODE = True
    CATEGORY = "utils"

    def download_plugin(self, plugin_name):
        # Real download handled via HTTP endpoint
        return ()


# Get the custom_nodes directory
def get_custom_nodes_directory():
    # Current file is in: /path/to/ComfyUI/custom_nodes/plugin_downloader/plugin_downloader.py
    # We need: /path/to/ComfyUI/custom_nodes/
    script_dir = os.path.dirname(os.path.realpath(__file__))  # Get current file's directory
    
    # Check if we're in a subdirectory of custom_nodes
    if 'custom_nodes' in script_dir:
        # Navigate up until we find custom_nodes directory
        current = script_dir
        while current and os.path.basename(current) != 'custom_nodes':
            parent = os.path.dirname(current)
            if parent == current:  # Reached root
                break
            current = parent
        
        if os.path.basename(current) == 'custom_nodes':
            return current
    
    # Fallback: assume we're one level below custom_nodes
    return os.path.dirname(script_dir)


# ---------------------------------------
# List all custom_nodes plugins
# ---------------------------------------
@PromptServer.instance.routes.get("/plugin_downloader/list")
async def list_plugins_endpoint(request):
    try:
        custom_nodes_dir = get_custom_nodes_directory()
        plugins = []
        
        if os.path.exists(custom_nodes_dir):
            items = os.listdir(custom_nodes_dir)
            
            for item in items:
                item_path = os.path.join(custom_nodes_dir, item)
                # Only include directories (plugins are typically directories)
                # Skip hidden directories and common non-plugin items
                if os.path.isdir(item_path) and not item.startswith('.') and item not in ['__pycache__']:
                    # Calculate directory size
                    total_size = 0
                    file_count = 0
                    for root, dirs, files in os.walk(item_path):
                        # Skip __pycache__ and other cache directories
                        dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git', 'node_modules']]
                        for file in files:
                            file_path = os.path.join(root, file)
                            try:
                                total_size += os.path.getsize(file_path)
                                file_count += 1
                            except:
                                pass
                    
                    plugins.append({
                        "name": item,
                        "size": total_size,
                        "file_count": file_count
                    })
        
        # Sort by name
        plugins.sort(key=lambda x: x["name"])
        
        return web.json_response({"plugins": plugins}, status=200)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ---------------------------------------
# Download a plugin as zip
# ---------------------------------------
@PromptServer.instance.routes.get("/plugin_downloader/download/{plugin_name:.+}")
async def download_plugin_endpoint(request):
    try:
        plugin_name = request.match_info["plugin_name"]
        custom_nodes_dir = get_custom_nodes_directory()
        
        try:
            plugin_path = safe_join(custom_nodes_dir, plugin_name)
        except ValueError:
            return web.json_response({"error": "Forbidden"}, status=403)
        
        # Check if plugin exists
        if not os.path.exists(plugin_path) or not os.path.isdir(plugin_path):
            return web.json_response({"error": "Plugin not found"}, status=404)
        
        # Create zip file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for root, dirs, files in os.walk(plugin_path):
                # Skip cache directories
                dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git', 'node_modules']]
                for file in files:
                    file_path = os.path.join(root, file)
                    # Get relative path for zip archive
                    arcname = os.path.relpath(file_path, os.path.dirname(plugin_path))
                    try:
                        zip_file.write(file_path, arcname)
                    except Exception as e:
                        print(f"Error adding {file_path} to zip: {e}")
        
        # Prepare zip file for download
        zip_buffer.seek(0)
        
        return web.Response(
            body=zip_buffer.read(),
            headers={
                'Content-Type': 'application/zip',
                'Content-Disposition': f'attachment; filename="{plugin_name}.zip"'
            }
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ---------------------------------------
# Upload a plugin zip -> extract into custom_nodes/
# ---------------------------------------
@PromptServer.instance.routes.post("/plugin_downloader/upload")
async def upload_plugin_endpoint(request):
    try:
        # Size check via Content-Length
        content_length = request.content_length or 0
        if content_length > MAX_UPLOAD_SIZE:
            return web.json_response(
                {"error": f"File too large (> {MAX_UPLOAD_SIZE // (1024*1024)} MB)"},
                status=413,
            )

        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return web.json_response({"error": "Missing upload field 'file'"}, status=400)

        filename = (field.filename or "").strip()
        if not filename.lower().endswith(".zip"):
            return web.json_response({"error": "Only .zip files are allowed"}, status=400)

        overwrite = request.query.get("overwrite", "0") == "1"

        # Read into memory with hard size cap
        buf = io.BytesIO()
        total = 0
        while True:
            chunk = await field.read_chunk(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_SIZE:
                return web.json_response(
                    {"error": f"File too large (> {MAX_UPLOAD_SIZE // (1024*1024)} MB)"},
                    status=413,
                )
            buf.write(chunk)
        buf.seek(0)

        # Validate zip
        try:
            zf = zipfile.ZipFile(buf)
        except zipfile.BadZipFile:
            return web.json_response({"error": "Invalid zip file"}, status=400)

        custom_nodes_dir = os.path.abspath(get_custom_nodes_directory())

        # Decide target plugin folder name:
        # 1) if all entries share a common top-level folder, use it
        # 2) otherwise use the zip filename (without .zip) as the folder
        names = [n for n in zf.namelist() if n and not n.startswith("__MACOSX/")]
        if not names:
            return web.json_response({"error": "Empty zip"}, status=400)

        tops = set()
        for n in names:
            first = n.split("/", 1)[0]
            tops.add(first)

        if len(tops) == 1 and any(n.startswith(list(tops)[0] + "/") for n in names):
            target_name = list(tops)[0]
            strip_prefix = ""  # keep structure as-is
        else:
            target_name = os.path.splitext(os.path.basename(filename))[0]
            strip_prefix = ""

        # Forbid special/relative names
        if target_name in ("", ".", "..") or "/" in target_name or "\\" in target_name:
            return web.json_response({"error": "Illegal plugin name in zip"}, status=400)

        try:
            target_dir = safe_join(custom_nodes_dir, target_name)
        except ValueError:
            return web.json_response({"error": "Forbidden target path"}, status=403)

        if os.path.exists(target_dir):
            if not overwrite:
                return web.json_response(
                    {"error": f"Plugin '{target_name}' already exists. Retry with overwrite=1 to replace."},
                    status=409,
                )
            shutil.rmtree(target_dir, ignore_errors=True)

        # Safe extraction with zip-slip defense
        extracted_files = 0
        for member in zf.infolist():
            member_name = member.filename
            if member_name.startswith("__MACOSX/") or member_name.endswith("/.DS_Store"):
                continue
            # Normalize path
            rel = member_name
            if not tops or (len(tops) == 1 and rel.startswith(list(tops)[0] + "/")):
                rel = rel.split("/", 1)[1] if "/" in rel else ""
            if rel == "":
                continue
            try:
                dest_path = safe_join(target_dir, rel)
            except ValueError:
                return web.json_response({"error": "Malicious path detected in zip"}, status=400)
            if member.is_dir():
                os.makedirs(dest_path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with zf.open(member) as src, open(dest_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_files += 1

        zf.close()

        return web.json_response(
            {
                "message": f"Successfully installed plugin '{target_name}' ({extracted_files} files). Please restart ComfyUI to load it.",
                "plugin": target_name,
                "files": extracted_files,
            },
            status=200,
        )
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ---------------------------------------
# Web UI (HTML embedded)
# ---------------------------------------
@PromptServer.instance.routes.get("/plugin_downloader")
async def serve_plugin_downloader_page(request):
    html_content = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>ComfyUI 插件下载器</title>
<style>
body{font-family:Arial, sans-serif;max-width:1000px;margin:0 auto;background:#f5f5f5;padding:20px;}
.container{background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.1);}
h1{text-align:center;color:#333;}
.filter-box{margin-bottom:15px;}
.filter-box input{width:100%;padding:10px;border:1px solid #ddd;border-radius:4px;box-sizing:border-box;}
.button-group{margin-bottom:15px;}
button{background-color:#4CAF50;color:white;padding:10px 20px;border:none;border-radius:4px;cursor:pointer;font-size:14px;}
button:hover{background-color:#45a049;}
button:disabled{background-color:#ccc;cursor:not-allowed;}
.refresh-btn{background-color:#5bc0de;margin-right:10px;}
.refresh-btn:hover{background-color:#31b0d5;}
.download-all-btn{background-color:#f0ad4e;}
.download-all-btn:hover{background-color:#ec971f;}
.download-btn{background-color:#0275d8;}
.download-btn:hover{background-color:#0260d0;}
.plugin-item{padding:15px;border-bottom:1px solid #eee;display:flex;justify-content:space-between;align-items:center;}
.plugin-item:hover{background-color:#f9f9f9;}
.plugin-info{flex-grow:1;}
.plugin-name{font-weight:bold;font-size:16px;color:#333;margin-bottom:5px;}
.plugin-details{font-size:14px;color:#666;}
.progress{margin-top:10px;padding:10px;background-color:#e7f3ff;border-radius:4px;display:none;}
.error{background-color:#f2dede;color:#a94442;border:1px solid #ebccd1;padding:15px;border-radius:4px;margin-top:15px;display:none;}
</style>
</head>
<body>
<div class="container">
<h1>ComfyUI 插件下载器</h1>

<div class="filter-box">
<input type="text" id="filterInput" placeholder="搜索插件名称..." onkeyup="filterPlugins()">
</div>

<div class="button-group">
<button id="refreshBtn" class="refresh-btn">刷新列表</button>
<button id="downloadAllBtn" class="download-all-btn">下载所有插件</button>
<input type="file" id="uploadInput" accept=".zip" style="display:none">
<button id="uploadBtn" class="download-btn">上传 ZIP 安装插件</button>
</div>

<div id="errorDiv" class="error"></div>
<div id="progressDiv" class="progress"></div>
<div id="pluginList">加载中...</div>
</div>

<script>
const baseUrl = window.location.origin;
let allPlugins = [];

async function loadPluginList() {
    const pluginListDiv = document.getElementById('pluginList');
    const errorDiv = document.getElementById('errorDiv');
    errorDiv.style.display = 'none';
    
    try {
        const response = await fetch(`${baseUrl}/plugin_downloader/list`);
        const data = await response.json();
        
        if (response.ok) {
            if (data.plugins && data.plugins.length > 0) {
                allPlugins = data.plugins;
                renderPluginList(allPlugins);
            } else {
                pluginListDiv.innerHTML = '<p>暂无插件</p>';
                allPlugins = [];
            }
        } else {
            errorDiv.textContent = `加载失败: ${data.error}`;
            errorDiv.style.display = 'block';
        }
    } catch (error) {
        errorDiv.textContent = `网络错误: ${error.message}`;
        errorDiv.style.display = 'block';
    }
}

function renderPluginList(plugins) {
    const pluginListDiv = document.getElementById('pluginList');
    if (plugins.length === 0) {
        pluginListDiv.innerHTML = '<p>没有匹配的插件</p>';
        return;
    }
    let html = '';
    plugins.forEach(plugin => {
        const sizeInMB = (plugin.size / (1024 * 1024)).toFixed(2);
        html += `<div class="plugin-item">
            <div class="plugin-info">
                <div class="plugin-name">${plugin.name}</div>
                <div class="plugin-details">${sizeInMB} MB · ${plugin.file_count} 个文件</div>
            </div>
            <button class="download-btn" onclick="downloadPlugin('${plugin.name.replace(/'/g, "\\'")}')">下载 ZIP</button>
        </div>`;
    });
    pluginListDiv.innerHTML = html;
}

function filterPlugins() {
    const filterText = document.getElementById('filterInput').value.toLowerCase();
    if (filterText === '') {
        renderPluginList(allPlugins);
    } else {
        renderPluginList(allPlugins.filter(p => p.name.toLowerCase().includes(filterText)));
    }
}

function downloadPlugin(pluginName) {
    const link = document.createElement('a');
    link.href = `${baseUrl}/plugin_downloader/download/${encodeURIComponent(pluginName)}`;
    link.download = `${pluginName}.zip`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
}

async function downloadAllPlugins() {
    if (allPlugins.length === 0) { alert('没有可下载的插件'); return; }
    if (!confirm(`确定要下载所有 ${allPlugins.length} 个插件吗？`)) return;
    
    const progressDiv = document.getElementById('progressDiv');
    const downloadAllBtn = document.getElementById('downloadAllBtn');
    downloadAllBtn.disabled = true;
    progressDiv.style.display = 'block';
    
    for (let i = 0; i < allPlugins.length; i++) {
        progressDiv.textContent = `正在下载 ${i + 1}/${allPlugins.length}: ${allPlugins[i].name}`;
        downloadPlugin(allPlugins[i].name);
        await new Promise(r => setTimeout(r, 800));
    }
    
    progressDiv.textContent = `完成！已下载 ${allPlugins.length} 个插件`;
    downloadAllBtn.disabled = false;
    setTimeout(() => { progressDiv.style.display = 'none'; }, 3000);
}

async function uploadPlugin(file, overwrite) {
    const progressDiv = document.getElementById('progressDiv');
    const errorDiv = document.getElementById('errorDiv');
    errorDiv.style.display = 'none';
    progressDiv.style.display = 'block';
    progressDiv.textContent = `正在上传 ${file.name} ...`;

    const form = new FormData();
    form.append('file', file);
    const url = `${baseUrl}/plugin_downloader/upload` + (overwrite ? '?overwrite=1' : '');

    try {
        const resp = await fetch(url, { method: 'POST', body: form });
        const data = await resp.json();
        if (resp.status === 409 && !overwrite) {
            if (confirm(`插件已存在：${data.error}\n是否覆盖安装？`)) {
                return uploadPlugin(file, true);
            }
            progressDiv.style.display = 'none';
            return;
        }
        if (!resp.ok) {
            errorDiv.textContent = `上传失败: ${data.error || resp.status}`;
            errorDiv.style.display = 'block';
            progressDiv.style.display = 'none';
            return;
        }
        progressDiv.textContent = data.message || '上传成功';
        loadPluginList();
        setTimeout(() => { progressDiv.style.display = 'none'; }, 4000);
    } catch (e) {
        errorDiv.textContent = `网络错误: ${e.message}`;
        errorDiv.style.display = 'block';
        progressDiv.style.display = 'none';
    }
}

document.getElementById('uploadBtn').onclick = () => document.getElementById('uploadInput').click();
document.getElementById('uploadInput').onchange = (e) => {
    const f = e.target.files && e.target.files[0];
    if (f) uploadPlugin(f, false);
    e.target.value = '';
};

document.getElementById('refreshBtn').onclick = loadPluginList;
document.getElementById('downloadAllBtn').onclick = downloadAllPlugins;
window.onload = loadPluginList;
</script>
</body>
</html>
"""
    return web.Response(text=html_content, content_type="text/html")


# ---------------------------------------
# Node mappings
# ---------------------------------------
NODE_CLASS_MAPPINGS = {
    "PluginDownloader": PluginDownloader
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PluginDownloader": "Plugin Downloader"
}
