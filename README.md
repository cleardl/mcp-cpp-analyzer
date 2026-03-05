# MCP C++ Analyzer

## 项目背景

使用AI 编程工具（如 GitHub Copilot、Cursor）辅助C++大型项目开发面临诸多局限性：

**依赖处理效率低下**：为了解决单个用户的问题，现有AI编程工具常常会整体引入整个头文件，甚至包含无关的源代码片段，如果引入的源码文件很大，那么就会导致上下文窗口和Token资源被大量占用，信息冗余，且容易引发理解混乱和响应延迟。 

**上下文感知能力不足**：现有工具受限于上下文窗口大小，当项目代码超出窗口限制时，会对历史聊天结论内容进行文本压缩或信息截断，导致先前的分析信息丢失。

**静态分析工具集成不足**：在需要获取符号信息时，往往采用简单的文件内容搜索方式，缺乏与clangd等专业静态分析工具的深度集成，导致效率低下和分析结果不够精准。

## 项目介绍

MCP-CPP-Analyzer 是一个专为C++开发设计的Model Context Protocol (MCP) 服务，通过深度整合clangd静态分析工具，为Claude Code、Cursor等支持MCP协议的AI编程工具提供精确的C++代码语义分析能力。该服务通过符号定义查找、引用追踪、悬停信息获取等功能，实现AI工具与专业C++分析工具的无缝集成，解决现有AI编程工具在大型C++项目中的应用局限。

## 安装

### 环境要求

- **Python >= 3.12**

先git clone到本地，然后安装：

```bash
uv sync //安装相关依赖
pip install -e .
```

> 请确保执行 `pip install` 时使用的 Python 版本 >= 3.12。可通过 `python --version` 确认。

## 使用方法

### 前置要求

1. **安装 clangd**：请确保已安装 clangd 并将其添加到系统环境变量（PATH）中
   
   - 下载地址：https://releases.llvm.org/download.html
   
2. **CPP项目生成 compile_commands.json**：确保项目根目录包含 `compile_commands.json` 文件
   
   - CMake项目（非visual studio项目）：`cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
   
   - visual studio项目：需要使用工具将`sln`文件转换成 compile_commands.json
   
     > [CMAKE_EXPORT_COMPILE_COMMANDS — CMake 4.1.0-rc3 Documentation](https://cmake.org/cmake/help/latest/variable/CMAKE_EXPORT_COMPILE_COMMANDS.html)

### MCP 客户端连接

#### Cursor 配置

在 C++ 项目下创建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "cpp-analyzer": {
      "command": "mcp-cpp-analyzer",
      "args": [
        "serve",
        "--transport", "stdio",
        "--compile-db-path", "<compile_commands.json所在目录>"
      ]
    }
  }
}
```

### 安装 Cursor Rule（可选，推荐）

将内置的 Cursor 规则文件安装到目标 C++ 项目中，引导 AI 优先使用 MCP 工具进行精确的符号分析：

```bash
# 安装到指定项目目录
mcp-cpp-analyzer install-rule /path/to/your/cpp-project

# 安装到当前目录
mcp-cpp-analyzer install-rule
```

规则文件会被复制到 `<目标目录>/.cursor/rules/` 下，已存在的同名文件不会被覆盖。

## 当前已支持的能力

服务器提供以下 MCP 工具：

### `get_definition`
查找符号定义位置

### `get_references` 
查找符号引用

### `get_hover`
获取符号的悬停信息（类型、文档等）

### `diagnose_clangd`
诊断 `clangd `服务器状态和响应能力
