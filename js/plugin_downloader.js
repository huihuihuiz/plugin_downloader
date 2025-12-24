import { app } from "/scripts/app.js";

// 添加一个菜单项来打开插件下载器
app.registerExtension({
    name: "PluginDownloader.Extension",
    async setup() {
        // 创建一个新的菜单项
        const menu = document.querySelector(".comfy-menu");
        if (menu) {
            const separator = document.createElement("hr");
            separator.style.margin = "10px 0";
            menu.appendChild(separator);
            
            const button = document.createElement("button");
            button.textContent = "插件下载器";
            button.onclick = () => {
                // 打开新的窗口或标签页显示插件下载器
                window.open("/plugin_downloader", "_blank");
            };
            menu.appendChild(button);
        }
    }
});

// 注册静态文件路由
async function registerStaticRoutes() {
    try {
        console.log("Plugin Downloader extension loaded");
    } catch (error) {
        console.error("Failed to register static routes:", error);
    }
}

registerStaticRoutes();
