# MCP C++ Analyzer

面向 AI Agent 的 C/C++ 语义查询服务。通过 clangd 和 compile_commands.json，提供批量符号搜索、定义与引用、调用关系、类型层级和当前文档诊断。

## 二进制部署

Windows 发布程序使用 stdio 轻量代理，默认启用 Code Mode。Python、FastMCP 和 Code Mode 运行依赖已随包提供，使用端无需安装 Python 或保留源码。

发布目录的 bin 中包含 mcp-cpp-analyzer-proxy.exe、mcp-cpp-analyzer-backend.exe、_proxy 运行库和 clangd.exe；保留与 bin 相邻的 lib/clang 资源目录。客户端配置示例：

~~~json
{
  "mcpServers": {
    "cpp-analyzer": {
      "command": "C:/tools/cpp-analyzer/bin/mcp-cpp-analyzer-proxy.exe",
      "cwd": "C:/work/cpp-project",
      "env": {
        "CPP_ANALYZER_COMPILE_DB_PATH": "C:/work/build"
      }
    }
  }
}
~~~

编译数据库目录须包含 compile_commands.json；相对路径以 cwd 为基准。源码和协议文本固定使用 UTF-8，解码失败直接报错。服务不检查更新，不需要额外后端配置文件或启动参数。

启动超时建议 90 秒，工具超时建议 150 秒。首次连接通过 execute 调用 workspace_status 核对工作区、数据库、shared_backend.pid 与 clangd_pid。

## 共享后台生命周期

- 每个客户端只启动标准库代理。同一 Windows 用户、安装目录、源码目录、编译数据库、工具模式和程序构建复用一个后台，clangd、索引、符号句柄与有界结果缓存均共享；不同工作区隔离。
- 首个代理通过互斥锁创建并运行用户级按需任务，由任务启动独立后台，不依附首个客户端的 Job Object。没有登录、开机或定时触发器；启动失败直接报告 Task Scheduler 错误。
- 关闭一个客户端只断开它的连接。最后一个代理断开 30 秒后，后台关闭 clangd、删除按需任务并退出；期间重新接入会复用已有后台。
- 后台异常退出时，Windows Job Object 清理其子进程；现有代理重连并恢复协议会话，无需重启 Agent。进行中的查询返回 BACKEND_RESTARTED，不重放；旧句柄需重新发现。

后台仅监听本机回环地址，代理和后台双向认证。运行状态位于当前用户 LocalAppData 下的 mcp-cpp-analyzer，目录仅当前用户与 SYSTEM 可访问；端点和任务元数据由程序生成，无需另写配置。workspace_status.shared_backend.runtime_directory 可用于定位日志。

## 查询接口

客户端暴露 search、get_schema、execute。先发现接口，再在 execute 中通过 await call_tool(name, arguments) 批量查询、筛选并 return：

| 内部工具 | 用途 |
| --- | --- |
| search_symbols | 批量名称发现、路径和 SymbolKind 过滤，返回 symbol_id |
| query_symbols | targets × operations 批量语义查询，最多 64 组 |
| query_files | 多文件符号大纲与当前文档版本诊断 |
| read_results | 批量分页或按 result_id 展开缓存结果 |
| workspace_status | clangd 能力、进程失败、工作区和索引活动 |

query_symbols 支持 definition、declaration、implementation、type_definition、hover、references、callers、callees、supertypes、subtypes、context、symbol_info。

~~~python
result = await call_tool("query_symbols", {
    "targets": [{"file": "src/client.cpp", "line": 42, "column": 8}],
    "operations": ["definition", "callers", "callees"],
    "fields": ["file", "line", "column", "name", "symbol_id"],
    "limit": 10,
})
return result
~~~

优先复用符号发现返回的 symbol_id。位置从 1 开始，列按 Unicode codepoint 计数，制表符计为一个字符。

- compact 使用 files 与 columns/rows；fields 投影所需列，保留文件映射与分页、错误信息。
- standard 返回具名对象，full 按需包含详细范围和源码。默认输出预算为 80 项、24000 字符。
- truncated 时用 cursor 继续；result_id 可从头展开，二者传给 read_results 的 cursors。
- 文件变化、句柄过期或进程重启后重新发现。部分失败逐项返回，全部失败使用 MCP isError=true。
- 跨文件结果依赖 clangd 索引，空结果不能证明全仓不存在，诊断不等同工程编译。
- index_activity 区分未观察到索引、索引中和空闲；空闲不代表全工程覆盖，index_complete 保持未知。空的索引查询返回 INDEX_COVERAGE_UNVERIFIED。
- definition 的 location_kind 区分 AST 定义、声明和索引位置；仅有声明时返回 IMPLEMENTATION_UNCONFIRMED。
- 在定义处查询时，clangd 可能回跳声明；同一 AST 已知定义时归一到定义位置，并标记 declaration_navigation=true。
- 调用结果保留调用点。能够用调用点 AST 确认定义时优先使用该位置；与索引冲突时同时保留 index_location 和警告，fields 也不能隐藏冲突证据。linkage_verified=false 表示未验证最终链接选择。

Code Mode 每次最多 50 次内部工具调用、120 秒。CPP_ANALYZER_TOOL_MODE 默认为 code；direct 或 search 需显式设置。

Code Mode 对对象结果只保留一份 structuredContent，不附加相同 JSON 的文本副本；非结构化结果与工具错误保留 content。fields 的允许值同时提供在完整 JSON schema 和简略参数说明中。

## 开发者构建

构建需要 Python 3.13+ 和 uv，使用 PyInstaller 收集解释器与依赖，不编译为 C。打包工具通过 build 依赖组锁定，与使用端无关：

~~~powershell
uv sync --locked --group build
.venv/Scripts/python.exe -X utf8 -B build.py --output dist
~~~

构建前须提交源码并保持工作区干净；代理与后台嵌入相同版本和 Git revision，通过 workspace_status.build 可核对来源。输出两个 exe、_proxy 运行库和第三方许可清单；clangd 及资源目录单独随包发布。

代理在导入运行逻辑前保留独立协议管道，将 Python print 与原生 stdout 日志导向 stderr；不导入 FastMCP、Pydantic 或沙箱。后台通过独立连接收发协议，日志写入运行目录，Monty worker 随后台封装。发布前验证多会话复用、工作区隔离、首个客户端退出、后台异常恢复、空闲清理及真实语义查询。

回归检查使用 stdlib unittest，只启动 clangd 解析测试文件，不调用 C/C++ 编译器。设置 CPP_ANALYZER_TEST_CLANGD 指向 clangd 后运行：

~~~powershell
.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
~~~
