# MCP C++ Analyzer

## 项目背景

使用AI 编程工具（如 GitHub Copilot、Cursor）辅助C++大型项目开发面临诸多局限性：

**依赖处理效率低下**：为了解决单个用户的问题，现有AI编程工具常常会整体引入整个头文件，甚至包含无关的源代码片段，如果引入的源码文件很大，那么就会导致上下文窗口和Token资源被大量占用，信息冗余，且容易引发理解混乱和响应延迟。 

**上下文感知能力不足**：现有工具受限于上下文窗口大小，当项目代码超出窗口限制时，会对历史聊天结论内容进行文本压缩或信息截断，导致先前的分析信息丢失。

**静态分析工具集成不足**：在需要获取符号信息时，往往采用简单的文件内容搜索方式，缺乏与clangd等专业静态分析工具的深度集成，导致效率低下和分析结果不够精准。

## 项目介绍

MCP-CPP-Analyzer 是一个专为C++开发设计的Model Context Protocol (MCP) 服务，通过深度整合clangd静态分析工具，为Claude Code、Cursor等支持MCP协议的AI编程工具提供精确的C++代码语义分析能力。该服务通过符号定义查找、引用追踪、悬停信息获取等功能，实现AI工具与专业C++分析工具的无缝集成，解决现有AI编程工具在大型C++项目中的应用局限。

## 安装

先git clone到本地

```bash
pip install -e .
```

## 使用方法

### 前置要求

1. **安装 clangd**：请确保已安装 clangd 并将其添加到系统环境变量（PATH）中
   
   - 下载地址：https://releases.llvm.org/download.html
   
2. **生成 compile_commands.json**：确保项目根目录包含 `compile_commands.json` 文件
   
   - CMake项目（非visual studio项目）：`cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON`
   
   - visual studio项目：需要使用工具将`sln`文件转换成 compile_commands.json
   
     > [CMAKE_EXPORT_COMPILE_COMMANDS — CMake 4.1.0-rc3 Documentation](https://cmake.org/cmake/help/latest/variable/CMAKE_EXPORT_COMPILE_COMMANDS.html)

### 启动服务

安装完成后，可以使用以下命令启动最简单形式服务器：

```bash
mcp-cpp-analyzer <compile_commands.json所在目录>
```

#### 命令参数

```bash
mcp-cpp-analyzer --compile-db-path <directory> [options]

选项:
  --compile-db-path DIR C++ compile-commands.json所在路径 (必需)
  --clangd-path PATH    clangd 可执行文件路径
  --log-level LEVEL     日志级别 [debug|info|warning|error] (默认: info)
  --init-timeout SECONDS   LSP 初始化超时时间
  --hover-timeout SECONDS   悬停信息超时时间
  --definition-timeout SECONDS   定义查找超时时间
  --references-timeout SECONDS   引用查找超时时间
  --host HOST           服务器绑定地址 (默认: 127.0.0.1)
  --port PORT           服务器端口 (默认: 8000)
```

#### 启动示例

```bash
# 启动基本服务
mcp-cpp-analyzer /home/user/my-cpp-project

# 指定 clangd 路径
mcp-cpp-analyzer /home/user/my-cpp-project --clangd-path /usr/local/bin/clangd

# 启用调试日志
mcp-cpp-analyzer --compile-db-path /home/user/my-cpp-project --log-level debug

# 自定义超时设置
mcp-cpp-analyzer --compile-db-path /home/user/my-cpp-project --hover-timeout 15 --definition-timeout 25

# 自定义服务器地址和端口
mcp-cpp-analyzer --compile-db-path /home/user/my-cpp-project --host 0.0.0.0 --port 9000
```

### MCP客户端连接

服务器启动后，MCP客户端可以通过以下方式连接：

- **SSE 连接**: `http://127.0.0.1:8000/sse` (默认)
- **stdio**

#### Cursor配置

当前项目下创建`./cursor/mcp.json`添加配置：

```
{
  "mcpServers": {
    "cpp-analyzer": {
      "command": "mcp-cpp-analyzer",
      "args": [
        "--transport", "stdio",
        "--compile-db-path", <compile_commands.json所在目录>
      ]
    }
  }
}

```



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
