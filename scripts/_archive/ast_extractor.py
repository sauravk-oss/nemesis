#!/usr/bin/env python3
"""AST Extractor — Go-focused code intelligence parser using grep patterns.
Extracts functions, structs, interfaces, imports, routes, DB operations, tests, and call relationships."""

import os
import re
import json
import sys

VENDOR_DIRS = {"vendor", ".git", "node_modules", "mock", "mocks", "testdata"}

def should_skip(path):
    parts = path.split(os.sep)
    return any(p in VENDOR_DIRS for p in parts)

def find_go_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VENDOR_DIRS]
        for f in filenames:
            if f.endswith(".go"):
                fpath = os.path.join(dirpath, f)
                if not should_skip(fpath):
                    files.append(fpath)
    return sorted(files)

def get_package(content):
    m = re.search(r'^package\s+(\w+)', content, re.MULTILINE)
    return m.group(1) if m else ""

def get_relative_path(fpath, root):
    return os.path.relpath(fpath, root)

def extract_imports(content, fpath, root, package):
    imports = []
    single = re.findall(r'^\s*import\s+"([^"]+)"', content, re.MULTILINE)
    for imp in single:
        imports.append({
            "module": imp,
            "file": get_relative_path(fpath, root),
            "importer": package,
            "external": not imp.startswith("github.com/razorpay/"),
        })
    block = re.findall(r'import\s*\((.*?)\)', content, re.DOTALL)
    for b in block:
        for line in b.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            m = re.search(r'"([^"]+)"', line)
            if m:
                imp = m.group(1)
                imports.append({
                    "module": imp,
                    "file": get_relative_path(fpath, root),
                    "importer": package,
                    "external": not imp.startswith("github.com/razorpay/"),
                })
    return imports

def calc_complexity(body):
    score = 1
    score += len(re.findall(r'\bif\b', body))
    score += len(re.findall(r'\belse\b', body))
    score += len(re.findall(r'\bfor\b', body))
    score += len(re.findall(r'\bswitch\b', body))
    score += len(re.findall(r'\bcase\b', body))
    score += len(re.findall(r'\bselect\b', body))
    score += len(re.findall(r'\b&&\b', body))
    score += len(re.findall(r'\b\|\|\b', body))
    score += len(re.findall(r'\bgo\b', body))
    score += len(re.findall(r'\bdefer\b', body))
    normalized = min(score / 30.0, 1.0)
    return round(normalized, 2)

def extract_function_body(content, start_pos):
    brace_count = 0
    in_body = False
    i = start_pos
    body_start = -1
    while i < len(content):
        ch = content[i]
        if ch == '{':
            if not in_body:
                body_start = i
                in_body = True
            brace_count += 1
        elif ch == '}':
            brace_count -= 1
            if in_body and brace_count == 0:
                return content[body_start:i+1]
        i += 1
    return content[body_start:] if body_start >= 0 else ""

def extract_functions(content, fpath, root, package):
    functions = []
    pattern = re.compile(
        r'^func\s+(?:\((\w+)\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)\s*([^{]*)',
        re.MULTILINE
    )
    for m in pattern.finditer(content):
        receiver_type = m.group(2) or ""
        func_name = m.group(3)
        params_str = m.group(4)
        returns_str = m.group(5).strip()
        line = content[:m.start()].count('\n') + 1

        full_name = f"{receiver_type}.{func_name}" if receiver_type else func_name
        if package:
            qualified = f"{package}.{full_name}"
        else:
            qualified = full_name

        body = extract_function_body(content, m.start())
        complexity = calc_complexity(body)

        params = []
        if params_str.strip():
            for p in params_str.split(","):
                p = p.strip()
                if p:
                    parts = p.rsplit(None, 1)
                    params.append(p)

        functions.append({
            "name": qualified,
            "file": get_relative_path(fpath, root),
            "line": line,
            "end_line": line + body.count('\n'),
            "language": "go",
            "complexity": complexity,
            "params": params,
            "returns": returns_str,
            "receiver": f"{receiver_type}" if receiver_type else "",
            "package": package,
            "body": body,
            "body_length": len(body),
            "is_test": func_name.startswith("Test") or func_name.startswith("Benchmark"),
            "is_exported": func_name[0].isupper() if func_name else False,
        })
    return functions

def extract_structs(content, fpath, root, package):
    structs = []
    pattern = re.compile(r'^type\s+(\w+)\s+struct\s*\{', re.MULTILINE)
    for m in pattern.finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        body = extract_function_body(content, m.start())
        fields = re.findall(r'(\w+)\s+(\S+)', body)
        structs.append({
            "name": f"{package}.{name}" if package else name,
            "file": get_relative_path(fpath, root),
            "line": line,
            "language": "go",
            "methods": [],
            "fields": [{"name": f[0], "type": f[1]} for f in fields if f[0][0].isupper() or f[0][0].islower()],
            "package": package,
        })
    return structs

def extract_interfaces(content, fpath, root, package):
    interfaces = []
    pattern = re.compile(r'^type\s+(\w+)\s+interface\s*\{', re.MULTILINE)
    for m in pattern.finditer(content):
        name = m.group(1)
        line = content[:m.start()].count('\n') + 1
        body = extract_function_body(content, m.start())
        methods = re.findall(r'(\w+)\s*\(', body)
        interfaces.append({
            "name": f"{package}.{name}" if package else name,
            "file": get_relative_path(fpath, root),
            "line": line,
            "language": "go",
            "methods": methods,
            "package": package,
        })
    return interfaces

def extract_routes(content, fpath, root, package):
    routes = []
    seen = set()  # dedup by (path, method)

    # --- Classic net/http and gorilla/mux patterns ---
    classic_patterns = [
        re.compile(r'\.HandleFunc\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)'),
        re.compile(r'\.Handle\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)'),
        re.compile(r'router\.(?:HandleFunc|Handle)\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)\.Methods\(\s*"(\w+)"'),
        re.compile(r'Path\(\s*"([^"]+)"\s*\).*?HandlerFunc\(\s*(\w+(?:\.\w+)*)\s*\)'),
    ]

    # --- chi router patterns (used by many Razorpay services) ---
    chi_patterns = [
        # r.Get("/path", handler)  r.Post("/path", handler)  etc.
        re.compile(r'\.\s*(Get|Post|Put|Delete|Patch|Head|Options)\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)'),
        # r.Route("/path", func(r chi.Router) { ... })
        re.compile(r'\.Route\(\s*"([^"]+)"\s*,'),
        # r.Mount("/prefix", subRouter)
        re.compile(r'\.Mount\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)'),
        # r.Group(func(r chi.Router) { ... })  — not a route itself, skip
    ]

    # --- gin framework patterns ---
    gin_patterns = [
        re.compile(r'\.\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\(\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)\s*\)'),
        re.compile(r'\.Group\(\s*"([^"]+)"'),
    ]

    # --- spine (Razorpay internal framework) patterns ---
    spine_patterns = [
        # spine route registration
        re.compile(r'AddRoute\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\w+(?:\.\w+)*)'),
        re.compile(r'RegisterRoute\(\s*"([^"]+)"\s*,\s*"([^"]+)"'),
        re.compile(r'spine\.(?:Route|Handle)\(\s*"([^"]+)"\s*,\s*"([^"]+)"'),
    ]

    # --- gRPC service registration ---
    grpc_patterns = [
        # pb.RegisterXServiceServer(grpcServer, &server{})
        re.compile(r'pb\.Register(\w+Server)\s*\(\s*\w+\s*,'),
        # RegisterXServiceServer(s, &server{})
        re.compile(r'Register(\w+Server)\s*\(\s*\w+\s*,'),
        # gRPC service definition in Go (generated from proto)
        re.compile(r'type\s+(\w+Server)\s+interface\s*\{'),
    ]

    # --- proto-style RPC definitions in .go files ---
    rpc_patterns = [
        # rpc method definitions: func (s *server) MethodName(ctx, req) (resp, error)
        re.compile(r'func\s+\(\w+\s+\*\w+\)\s+(\w+)\s*\(\s*ctx\s+context\.Context\s*,\s*\w+\s+\*\w+\.(\w+)\)'),
    ]

    method_pattern = re.compile(r'\.Methods\(\s*"(\w+)"')

    # Process classic patterns
    for pat in classic_patterns:
        for m in pat.finditer(content):
            path = m.group(1)
            handler = m.group(2)
            method = m.group(3) if m.lastindex >= 3 else ""
            if not method:
                ctx = content[m.end():m.end()+200]
                mm = method_pattern.search(ctx)
                method = mm.group(1) if mm else "ANY"
            key = (path, method.upper())
            if key not in seen:
                seen.add(key)
                routes.append({
                    "path": path, "method": method.upper(), "handler": handler,
                    "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                })

    # Process chi patterns
    for pat in chi_patterns:
        for m in pat.finditer(content):
            groups = m.groups()
            if len(groups) >= 3:  # .Get("/path", handler)
                method, path, handler = groups[0], groups[1], groups[2]
                key = (path, method.upper())
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": path, "method": method.upper(), "handler": handler,
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })
            elif len(groups) >= 2:  # .Mount("/prefix", sub)
                path, handler = groups[0], groups[1]
                key = (path, "MOUNT")
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": path, "method": "MOUNT", "handler": handler,
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })
            elif len(groups) >= 1:  # .Route("/path", ...)
                path = groups[0]
                key = (path, "ROUTE")
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": path, "method": "ROUTE", "handler": "",
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })

    # Process gin patterns
    for pat in gin_patterns:
        for m in pat.finditer(content):
            groups = m.groups()
            if len(groups) >= 3:
                method, path, handler = groups[0], groups[1], groups[2]
                key = (path, method.upper())
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": path, "method": method.upper(), "handler": handler,
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })
            elif len(groups) >= 1:  # .Group("/prefix")
                path = groups[0]
                key = (path, "GROUP")
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": path, "method": "GROUP", "handler": "",
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })

    # Process spine patterns
    for pat in spine_patterns:
        for m in pat.finditer(content):
            groups = m.groups()
            if len(groups) >= 3:
                method, path, handler = groups[0], groups[1], groups[2]
            elif len(groups) >= 2:
                path, method = groups[0], groups[1]
                handler = ""
            else:
                continue
            key = (path, method.upper())
            if key not in seen:
                seen.add(key)
                routes.append({
                    "path": path, "method": method.upper(), "handler": handler,
                    "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                })

    # Process gRPC service registrations
    for pat in grpc_patterns:
        for m in pat.finditer(content):
            svc_name = m.group(1)
            # Skip interface definitions for now, only capture registrations
            if 'interface' not in content[m.start():m.start()+50]:
                key = (f"grpc://{svc_name}", "GRPC")
                if key not in seen:
                    seen.add(key)
                    routes.append({
                        "path": f"grpc://{svc_name}", "method": "GRPC", "handler": svc_name,
                        "file": get_relative_path(fpath, root), "auth": "", "middleware": [],
                    })

    return routes

def extract_db_operations(content, fpath, root, package):
    ops = []
    seen = set()  # dedup by (table, operation, line)
    patterns = [
        # GORM table/from
        (re.compile(r'\.(?:Table|From)\(\s*"(\w+)"'), "QUERY"),
        # Raw SQL
        (re.compile(r'(?:db|tx|conn|repo|store|r)\.\s*(?:Exec|Query|QueryRow|QueryContext|ExecContext|RawQuery)\s*\([^)]*"[^"]*(?:FROM|JOIN)\s+[`"]?(\w+)', re.IGNORECASE), "QUERY"),
        (re.compile(r'(?:db|tx|conn|repo|store|r)\.\s*(?:Exec|Query|QueryRow|ExecContext)\s*\([^)]*"[^"]*INSERT\s+INTO\s+[`"]?(\w+)', re.IGNORECASE), "INSERT"),
        (re.compile(r'(?:db|tx|conn|repo|store|r)\.\s*(?:Exec|Query|ExecContext)\s*\([^)]*"[^"]*UPDATE\s+[`"]?(\w+)', re.IGNORECASE), "UPDATE"),
        (re.compile(r'(?:db|tx|conn|repo|store|r)\.\s*(?:Exec|Query|ExecContext)\s*\([^)]*"[^"]*DELETE\s+FROM\s+[`"]?(\w+)', re.IGNORECASE), "DELETE"),
        # String constants containing SQL table refs
        (re.compile(r'(?:SELECT|select)\s+.*?\s+(?:FROM|from)\s+[`"]?(\w+)'), "QUERY"),
        (re.compile(r'(?:INSERT|insert)\s+(?:INTO|into)\s+[`"]?(\w+)'), "INSERT"),
        (re.compile(r'(?:UPDATE|update)\s+[`"]?(\w+)\s+(?:SET|set)'), "UPDATE"),
        (re.compile(r'(?:DELETE|delete)\s+(?:FROM|from)\s+[`"]?(\w+)'), "DELETE"),
        # GORM model-based operations
        (re.compile(r'\.Create\(\s*&?\s*(\w+)'), "INSERT"),
        (re.compile(r'\.Save\(\s*&?\s*(\w+)'), "UPDATE"),
        (re.compile(r'\.Delete\(\s*&?\s*(\w+)'), "DELETE"),
        (re.compile(r'\.Find\(\s*&?\s*(\w+)'), "QUERY"),
        (re.compile(r'\.First\(\s*&?\s*(\w+)'), "QUERY"),
        (re.compile(r'\.Last\(\s*&?\s*(\w+)'), "QUERY"),
        (re.compile(r'\.Where\(\s*"([^"]+)"'), "QUERY"),
        (re.compile(r'\.Pluck\(\s*"(\w+)"'), "QUERY"),
        (re.compile(r'\.Count\(\s*&'), "QUERY"),
        # sqlx named queries
        (re.compile(r'\.NamedExec\s*\([^)]*"[^"]*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+[`"]?(\w+)', re.IGNORECASE), "WRITE"),
        (re.compile(r'\.NamedQuery\s*\([^)]*"[^"]*(?:FROM|JOIN)\s+[`"]?(\w+)', re.IGNORECASE), "QUERY"),
        # Redis operations
        (re.compile(r'\.(?:Get|Set|Del|HGet|HSet|LPush|RPush|SAdd|ZAdd)\s*\(\s*(?:ctx\s*,\s*)?"([^"]+)"'), "CACHE"),
        # Spine DB (Razorpay internal)
        (re.compile(r'spine\.(?:DB|Repo|Store)\s*\.\s*(?:Find|Create|Update|Delete|Query)\s*\('), "QUERY"),
        # Model() calls
        (re.compile(r'\.Model\(\s*&?\s*(\w+)\s*\{\s*\}'), "QUERY"),
    ]
    for pat, op_type in patterns:
        for m in pat.finditer(content):
            table = m.group(1) if m.lastindex and m.lastindex >= 1 else "unknown"
            # Skip common false positives
            if table in ('err', 'ctx', 'nil', 'true', 'false', 'ok', 'string', 'int', 'bool', 'error', 'interface', 'func', 'struct'):
                continue
            line = content[:m.start()].count('\n') + 1
            key = (table, op_type, line)
            if key not in seen:
                seen.add(key)
                ops.append({
                    "table": table,
                    "operation": op_type,
                    "file": get_relative_path(fpath, root),
                    "line": line,
                })
    return ops

def extract_calls(content, functions, package):
    calls = []
    func_names = {f["name"].split(".")[-1] for f in functions}
    for func in functions:
        short_name = func["name"].split(".")[-1]
        func_pattern = re.compile(rf'func\s+(?:\(\w+\s+\*?\w+\)\s+)?{re.escape(short_name)}\s*\(')
        fm = func_pattern.search(content)
        if not fm:
            continue
        body = extract_function_body(content, fm.start())
        call_pattern = re.compile(r'(?:(\w+)\.)?(\w+)\s*\(')
        for cm in call_pattern.finditer(body):
            callee = cm.group(2)
            receiver = cm.group(1) or ""
            if callee in func_names and callee != short_name:
                callee_qualified = f"{package}.{receiver}.{callee}" if receiver else f"{package}.{callee}"
                calls.append({
                    "caller": func["name"],
                    "callee": callee_qualified,
                })
    return calls

def extract_tests(functions):
    tests = []
    for f in functions:
        short = f["name"].split(".")[-1]
        if short.startswith("Test") and not short.startswith("TestMain"):
            tested = short[4:]
            if tested.startswith("_"):
                tested = tested[1:]
            tests.append({
                "test_name": f["name"],
                "tests_function": f"{f['package']}.{tested}" if f.get("package") else tested,
                "file": f["file"],
                "package": f.get("package", ""),
            })
    return tests

def extract_config_keys(content, fpath, root):
    configs = []
    patterns = [
        re.compile(r'viper\.Get(?:String|Int|Bool|Duration|Float64)?\(\s*"([^"]+)"'),
        re.compile(r'config\.Get(?:String|Int|Bool)?\(\s*"([^"]+)"'),
        re.compile(r'os\.Getenv\(\s*"([^"]+)"'),
    ]
    for pat in patterns:
        for m in pat.finditer(content):
            configs.append({
                "key": m.group(1),
                "file": get_relative_path(fpath, root),
                "env": "",
            })
    return configs

def find_php_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VENDOR_DIRS and d != 'storage' and d != 'bootstrap']
        for f in filenames:
            if f.endswith(".php") and not f.startswith('.'):
                fpath = os.path.join(dirpath, f)
                if not should_skip(fpath):
                    files.append(fpath)
    return sorted(files)

def find_ts_files(root):
    files = []
    skip = VENDOR_DIRS | {'dist', 'build', '.next', 'coverage', '__snapshots__'}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            if f.endswith(('.ts', '.tsx')) and not f.endswith('.d.ts'):
                fpath = os.path.join(dirpath, f)
                if not should_skip(fpath):
                    files.append(fpath)
    return sorted(files)

def find_proto_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in VENDOR_DIRS]
        for f in filenames:
            if f.endswith(".proto"):
                fpath = os.path.join(dirpath, f)
                if not should_skip(fpath):
                    files.append(fpath)
    return sorted(files)

def extract_php(content, fpath, root):
    """Extract functions, classes, routes, and DB ops from PHP files."""
    functions = []
    classes = []
    endpoints = []
    db_ops = []
    imports = []

    relpath = get_relative_path(fpath, root)

    # PHP namespace as package
    ns_m = re.search(r'namespace\s+([\w\\]+)\s*;', content)
    namespace = ns_m.group(1).replace('\\', '.') if ns_m else ""

    # PHP classes
    for m in re.finditer(r'class\s+(\w+)(?:\s+extends\s+(\w+))?(?:\s+implements\s+([\w,\s\\]+))?\s*\{', content):
        name = m.group(1)
        extends = m.group(2) or ""
        classes.append({
            "name": f"{namespace}.{name}" if namespace else name,
            "file": relpath, "line": content[:m.start()].count('\n') + 1,
            "language": "php", "methods": [], "fields": [],
            "package": namespace, "extends": extends,
        })

    # PHP functions/methods
    for m in re.finditer(r'(?:public|protected|private|static)?\s*function\s+(\w+)\s*\(([^)]*)\)', content):
        fname = m.group(1)
        line = content[:m.start()].count('\n') + 1
        is_test = fname.startswith('test') or fname.startswith('Test')
        body = extract_function_body(content, m.start())
        complexity = calc_complexity(body) if body else 0
        functions.append({
            "name": f"{namespace}.{fname}" if namespace else fname,
            "file": relpath, "line": line,
            "end_line": line + body.count('\n') if body else line,
            "language": "php",
            "complexity": complexity, "params": [], "returns": "",
            "receiver": "", "package": namespace,
            "body": body,
            "body_length": len(body), "is_test": is_test,
            "is_exported": not fname.startswith('_'),
        })

    # PHP use statements as imports
    for m in re.finditer(r'use\s+([\w\\]+)(?:\s+as\s+\w+)?;', content):
        imp = m.group(1).replace('\\', '/')
        imports.append({
            "module": imp, "file": relpath,
            "importer": namespace, "external": 'Razorpay' not in imp,
        })

    # Laravel/Lumen routes
    for m in re.finditer(r"Route::(get|post|put|delete|patch|any)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]?([^'\")\]]+)", content, re.IGNORECASE):
        endpoints.append({
            "path": m.group(2), "method": m.group(1).upper(),
            "handler": m.group(3), "file": relpath, "auth": "", "middleware": [],
        })

    # PHP DB operations (Eloquent / raw)
    for m in re.finditer(r"(?:DB::table|->table)\s*\(\s*['\"](\w+)['\"]", content):
        db_ops.append({"table": m.group(1), "operation": "QUERY", "file": relpath, "line": content[:m.start()].count('\n') + 1})
    for m in re.finditer(r"(?:->|DB::)select\s*\([^)]*['\"].*?FROM\s+(\w+)", content, re.IGNORECASE):
        db_ops.append({"table": m.group(1), "operation": "QUERY", "file": relpath, "line": content[:m.start()].count('\n') + 1})

    return functions, classes, endpoints, db_ops, imports

def extract_typescript(content, fpath, root):
    """Extract functions, classes, routes, and imports from TypeScript files."""
    functions = []
    classes = []
    endpoints = []
    imports = []

    relpath = get_relative_path(fpath, root)

    # Imports
    for m in re.finditer(r"import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", content):
        imports.append({
            "module": m.group(1), "file": relpath,
            "importer": "", "external": not m.group(1).startswith('.'),
        })

    # TS/JS classes
    for m in re.finditer(r'(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?\s*(?:implements\s+[\w,\s]+)?\s*\{', content):
        classes.append({
            "name": m.group(1), "file": relpath,
            "line": content[:m.start()].count('\n') + 1,
            "language": "typescript", "methods": [], "fields": [],
            "package": "", "extends": m.group(2) or "",
        })

    # Functions (named + arrow + methods)
    for m in re.finditer(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*(?:<[^>]+>)?\s*\(', content):
        body = extract_function_body(content, m.start())
        complexity = calc_complexity(body) if body else 0
        line = content[:m.start()].count('\n') + 1
        functions.append({
            "name": m.group(1), "file": relpath,
            "line": line,
            "end_line": line + body.count('\n') if body else line,
            "language": "typescript",
            "complexity": complexity, "params": [], "returns": "",
            "receiver": "", "package": "",
            "body": body,
            "body_length": len(body),
            "is_test": '.test.' in relpath or '.spec.' in relpath or m.group(1).startswith('test'),
            "is_exported": True,
        })
    # Arrow functions assigned to const
    for m in re.finditer(r'(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::\s*\w+)?\s*=>', content):
        body = extract_function_body(content, m.start())
        complexity = calc_complexity(body) if body else 0
        line = content[:m.start()].count('\n') + 1
        functions.append({
            "name": m.group(1), "file": relpath,
            "line": line,
            "end_line": line + body.count('\n') if body else line,
            "language": "typescript",
            "complexity": complexity, "params": [], "returns": "",
            "receiver": "", "package": "",
            "body": body,
            "body_length": len(body),
            "is_test": '.test.' in relpath or '.spec.' in relpath,
            "is_exported": True,
        })

    # Express/Koa routes
    for m in re.finditer(r"(?:app|router)\.\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]", content, re.IGNORECASE):
        endpoints.append({
            "path": m.group(2), "method": m.group(1).upper(),
            "handler": "", "file": relpath, "auth": "", "middleware": [],
        })

    # Next.js API routes (file path is the route)
    if '/pages/api/' in relpath or '/app/api/' in relpath:
        route_path = relpath.split('/pages/api/')[-1] if '/pages/api/' in relpath else relpath.split('/app/api/')[-1]
        route_path = '/' + route_path.replace('/route.ts', '').replace('/route.tsx', '').replace('.ts', '').replace('.tsx', '')
        endpoints.append({
            "path": route_path, "method": "ANY", "handler": relpath,
            "file": relpath, "auth": "", "middleware": [],
        })

    return functions, classes, endpoints, imports

def extract_proto(content, fpath, root):
    """Extract service definitions and RPC methods from .proto files."""
    functions = []
    classes = []
    endpoints = []

    relpath = get_relative_path(fpath, root)

    # Package
    pkg_m = re.search(r'package\s+([\w.]+)\s*;', content)
    package = pkg_m.group(1) if pkg_m else ""

    # Messages (like classes)
    for m in re.finditer(r'message\s+(\w+)\s*\{', content):
        classes.append({
            "name": f"{package}.{m.group(1)}" if package else m.group(1),
            "file": relpath, "line": content[:m.start()].count('\n') + 1,
            "language": "proto", "methods": [], "fields": [],
            "package": package,
        })

    # Services
    for m in re.finditer(r'service\s+(\w+)\s*\{', content):
        svc_name = m.group(1)
        body = extract_function_body(content, m.start())
        # RPC methods within service
        for rm in re.finditer(r'rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(\w+)\s*\)', body):
            rpc_name = rm.group(1)
            req_type = rm.group(2)
            resp_type = rm.group(3)
            functions.append({
                "name": f"{package}.{svc_name}.{rpc_name}" if package else f"{svc_name}.{rpc_name}",
                "file": relpath, "line": content[:m.start()].count('\n') + 1,
                "language": "proto", "complexity": 0,
                "params": [req_type], "returns": resp_type,
                "receiver": svc_name, "package": package,
                "body_length": 0, "is_test": False, "is_exported": True,
            })
            endpoints.append({
                "path": f"grpc://{svc_name}/{rpc_name}",
                "method": "GRPC", "handler": f"{svc_name}.{rpc_name}",
                "file": relpath, "auth": "", "middleware": [],
            })

    return functions, classes, endpoints

def analyze_repo(root):
    go_files = find_go_files(root)
    php_files = find_php_files(root)
    ts_files = find_ts_files(root)
    proto_files = find_proto_files(root)

    result = {
        "functions": [],
        "classes": [],
        "imports": [],
        "endpoints": [],
        "db_operations": [],
        "calls": [],
        "tests": [],
        "config": [],
        "interfaces": [],
        "stats": {
            "total_files": len(go_files) + len(php_files) + len(ts_files) + len(proto_files),
            "go_files": len(go_files),
            "php_files": len(php_files),
            "ts_files": len(ts_files),
            "proto_files": len(proto_files),
            "total_functions": 0,
            "total_structs": 0,
            "total_interfaces": 0,
            "total_endpoints": 0,
            "total_imports": 0,
            "total_tests": 0,
            "packages": set(),
        }
    }

    # --- Go files ---
    for fpath in go_files:
        try:
            with open(fpath, 'r', errors='replace') as f:
                content = f.read()
        except Exception:
            continue

        package = get_package(content)
        if package:
            result["stats"]["packages"].add(package)

        imports = extract_imports(content, fpath, root, package)
        result["imports"].extend(imports)

        functions = extract_functions(content, fpath, root, package)
        result["functions"].extend(functions)

        structs = extract_structs(content, fpath, root, package)
        result["classes"].extend(structs)

        interfaces = extract_interfaces(content, fpath, root, package)
        result["interfaces"].extend(interfaces)

        routes = extract_routes(content, fpath, root, package)
        result["endpoints"].extend(routes)

        db_ops = extract_db_operations(content, fpath, root, package)
        result["db_operations"].extend(db_ops)

        calls = extract_calls(content, functions, package)
        result["calls"].extend(calls)

        configs = extract_config_keys(content, fpath, root)
        result["config"].extend(configs)

    # --- PHP files ---
    for fpath in php_files:
        try:
            with open(fpath, 'r', errors='replace') as f:
                content = f.read()
        except Exception:
            continue
        fns, cls, eps, db_ops, imps = extract_php(content, fpath, root)
        result["functions"].extend(fns)
        result["classes"].extend(cls)
        result["endpoints"].extend(eps)
        result["db_operations"].extend(db_ops)
        result["imports"].extend(imps)

    # --- TypeScript files ---
    for fpath in ts_files:
        try:
            with open(fpath, 'r', errors='replace') as f:
                content = f.read()
        except Exception:
            continue
        fns, cls, eps, imps = extract_typescript(content, fpath, root)
        result["functions"].extend(fns)
        result["classes"].extend(cls)
        result["endpoints"].extend(eps)
        result["imports"].extend(imps)

    # --- Proto files ---
    for fpath in proto_files:
        try:
            with open(fpath, 'r', errors='replace') as f:
                content = f.read()
        except Exception:
            continue
        fns, cls, eps = extract_proto(content, fpath, root)
        result["functions"].extend(fns)
        result["classes"].extend(cls)
        result["endpoints"].extend(eps)

    test_funcs = extract_tests(result["functions"])
    result["tests"] = test_funcs

    result["stats"]["total_functions"] = len(result["functions"])
    result["stats"]["total_structs"] = len(result["classes"])
    result["stats"]["total_interfaces"] = len(result["interfaces"])
    result["stats"]["total_endpoints"] = len(result["endpoints"])
    result["stats"]["total_imports"] = len(result["imports"])
    result["stats"]["total_tests"] = len(result["tests"])
    result["stats"]["packages"] = sorted(result["stats"]["packages"])

    return result

def main():
    if len(sys.argv) < 2:
        print("Usage: ast_extractor.py <path> [--json]")
        sys.exit(1)

    target = sys.argv[1]
    as_json = "--json" in sys.argv

    if os.path.isdir(target):
        result = analyze_repo(target)
    elif os.path.isfile(target):
        result = analyze_repo(os.path.dirname(target))
    else:
        print(f"Error: {target} not found")
        sys.exit(1)

    if as_json:
        print(json.dumps(result, indent=2, default=list))
    else:
        stats = result["stats"]
        print(f"=== AST Extraction Summary ===")
        print(f"Files scanned:   {stats['total_files']}")
        print(f"Functions:       {stats['total_functions']}")
        print(f"Structs:         {stats['total_structs']}")
        print(f"Interfaces:      {stats['total_interfaces']}")
        print(f"Endpoints:       {stats['total_endpoints']}")
        print(f"Imports:         {stats['total_imports']}")
        print(f"Tests:           {stats['total_tests']}")
        print(f"Packages:        {len(stats['packages'])}")
        print(f"\nTop complex functions:")
        by_complexity = sorted(result["functions"], key=lambda x: x.get("complexity", 0), reverse=True)[:10]
        for f in by_complexity:
            print(f"  {f['complexity']:.2f}  {f['name']}  ({f['file']}:{f['line']})")

if __name__ == "__main__":
    main()
