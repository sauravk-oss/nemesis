#!/usr/bin/env node
/**
 * E2E Orchestrator MCP Server
 * Wraps razorpay/e2e-test-orchestrator Twirp HTTP API + ROAST Docker
 * for use inside Claude Desktop App.
 *
 * Twirp paths (actual, from proto):
 *   TestcaseAPI     → /twirp/rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI/
 *   TestExecutionAPI → /twirp/rzp.e2e_test_orchestrator.test_execution.v1.TestExecutionAPI/
 *   SuiteExecutionAPI → /twirp/rzp.e2e_test_orchestrator.suite_execution.v1.SuiteExecutionAPI/
 */

const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const { CallToolRequestSchema, ListToolsRequestSchema } = require("@modelcontextprotocol/sdk/types.js");
const http = require("http");
const https = require("https");
const { execSync, exec } = require("child_process");

const E2E_BASE = process.env.E2E_API_BASE || "https://e2e-test-orchestrator.dev.razorpay.in";
const E2E_USER = process.env.E2E_ORCHESTRATOR_USERNAME || "key";
const E2E_PASS = process.env.E2E_ORCHESTRATOR_PASSWORD || "secret";
const ROAST_IMAGE = process.env.ROAST_IMAGE || "roast:master";

// ── Twirp HTTP helper ─────────────────────────────────────────────────────────

async function twirpPost(path, body) {
  const url = `${E2E_BASE}${path}`;
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const lib = url.startsWith("https") ? https : http;
    const parsedUrl = new URL(url);
    const authHeader = "Basic " + Buffer.from(`${E2E_USER}:${E2E_PASS}`).toString("base64");
    const opts = {
      hostname: parsedUrl.hostname,
      port: parsedUrl.port || (url.startsWith("https") ? 443 : 80),
      path: parsedUrl.pathname,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(data),
        "Authorization": authHeader,
      },
    };
    const req = lib.request(opts, (res) => {
      let raw = "";
      res.on("data", (c) => (raw += c));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode, body: JSON.parse(raw) });
        } catch {
          resolve({ status: res.statusCode, body: raw });
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(10000, () => { req.destroy(new Error("Request timeout")); });
    req.write(data);
    req.end();
  });
}

// ── Tool definitions ──────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "e2e_health_check",
    description: "Check if e2e-test-orchestrator is running and healthy.",
    inputSchema: { type: "object", properties: {}, required: [] },
  },
  {
    name: "e2e_list_testcases",
    description: "List registered testcases, optionally filtered by service name.",
    inputSchema: {
      type: "object",
      properties: {
        service_name: { type: "string", description: "Filter by service slug (e.g. 'emandate-service')" },
        count: { type: "number", description: "Max results (default 50)" },
        skip: { type: "number", description: "Pagination offset" },
      },
      required: [],
    },
  },
  {
    name: "e2e_get_testcase",
    description: "Get a single testcase by id or name.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string" },
        name: { type: "string" },
        branch_ref: { type: "string", description: "end_to_end_tests_branch_ref" },
      },
    },
  },
  {
    name: "e2e_create_testcase",
    description: "Register a new testcase with the orchestrator.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Unique testcase name" },
        service_list: {
          type: "array",
          items: { type: "string" },
          description: "Services this testcase covers",
        },
        parent_service: { type: "string", description: "Primary service under test" },
        owner: { type: "string", description: "Team/owner identifier" },
        branch_ref: { type: "string", description: "Git branch for test code (default: main)" },
        priority: {
          type: "string",
          enum: ["P0", "P1", "P2"],
          description: "Test priority",
        },
      },
      required: ["name", "service_list"],
    },
  },
  {
    name: "e2e_run_testcase",
    description: "Execute a testcase by id. Returns execution_id to poll for results.",
    inputSchema: {
      type: "object",
      properties: {
        testcase_id: { type: "string", description: "Testcase ID from list or create" },
        execution_id: { type: "string", description: "Optional external execution tracking ID" },
      },
      required: ["testcase_id"],
    },
  },
  {
    name: "e2e_get_execution",
    description: "Get the status and results of a test execution.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Execution ID from run_testcase" },
      },
      required: ["id"],
    },
  },
  {
    name: "e2e_get_execution_history",
    description: "Get recent test execution history.",
    inputSchema: {
      type: "object",
      properties: {
        count: { type: "number", description: "Max results (default 20)" },
        service_name: { type: "string", description: "Filter by service" },
      },
    },
  },
  {
    name: "e2e_run_suite",
    description: "Create and run a suite execution (group of testcases).",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Suite name" },
        testcase_ids: {
          type: "array",
          items: { type: "string" },
          description: "List of testcase IDs to include",
        },
      },
      required: ["name", "testcase_ids"],
    },
  },
  {
    name: "e2e_run_roast",
    description: "Run ROAST (Razorpay's Java test suite) via Docker. Covers payments, UPI, cards, subscriptions.",
    inputSchema: {
      type: "object",
      properties: {
        env: {
          type: "string",
          enum: ["test", "prod"],
          description: "Test environment (default: test)",
        },
        mode: {
          type: "string",
          enum: ["intg", "conc"],
          description: "Test mode: intg=integration, conc=concurrency (default: intg)",
        },
        include_groups: {
          type: "string",
          description: "TestNG groups to run (e.g. 'PAYMENT,REFUND,SUBSCRIPTION,INVOICE')",
        },
        include_functions: {
          type: "string",
          description: "Specific test method names to run",
        },
        timeout_seconds: {
          type: "number",
          description: "Timeout in seconds (default: 600)",
        },
      },
    },
  },
  {
    name: "e2e_detect_local_method",
    description: "Detect the best local E2E runner for one or more services. Returns method (go_test/roast/none) and run command.",
    inputSchema: {
      type: "object",
      properties: {
        services: {
          type: "array",
          items: { type: "string" },
          description: "Service slugs to check (e.g. ['offers-engine', 'emandate-service'])",
        },
      },
      required: ["services"],
    },
  },
  {
    name: "e2e_run_service_pipeline",
    description: "Run E2E tests for ALL impacted services in parallel. Each service uses its best available runner (go test or ROAST). Results aggregated per service and ingested to brain.",
    inputSchema: {
      type: "object",
      properties: {
        feature_slug: { type: "string", description: "Feature slug from nemesis pipeline" },
        services: {
          type: "array",
          items: { type: "string" },
          description: "All impacted service slugs from solution.html",
        },
        env: {
          type: "string",
          enum: ["e2e", "test", "dev"],
          description: "Test environment (e2e=devstack, test=razorpay test env)",
        },
        devstack_label: {
          type: "string",
          description: "Devstack label for rzpctx-dev-serve-user header (e.g. 'saurav.k')",
        },
        timeout_per_service: {
          type: "number",
          description: "Seconds per service (default 300)",
        },
      },
      required: ["feature_slug", "services"],
    },
  },
  {
    name: "e2e_run_local",
    description: "Run E2E tests locally for a single service. Uses go test ./e2e/... or ROAST based on what's available.",
    inputSchema: {
      type: "object",
      properties: {
        service: { type: "string", description: "Service slug" },
        env: { type: "string", description: "APP_ENV value (default: e2e)" },
        devstack_label: { type: "string", description: "Devstack label header value" },
        timeout: { type: "number", description: "Timeout in seconds (default 300)" },
      },
      required: ["service"],
    },
  },
  {
    name: "e2e_ingest_results",
    description: "Ingest E2E test results into rubick.db knowledge graph (enriches brain context).",
    inputSchema: {
      type: "object",
      properties: {
        feature_slug: { type: "string", description: "Feature slug from nemesis pipeline" },
        service: { type: "string", description: "Service that was tested" },
        passed: { type: "number" },
        failed: { type: "number" },
        skipped: { type: "number" },
        duration_s: { type: "number" },
        failures: {
          type: "array",
          items: {
            type: "object",
            properties: {
              test: { type: "string" },
              error: { type: "string" },
            },
          },
        },
      },
      required: ["feature_slug", "service"],
    },
  },
];

// ── Tool handlers ─────────────────────────────────────────────────────────────

async function handleTool(name, args) {
  switch (name) {
    case "e2e_health_check": {
      try {
        const r = await twirpPost(
          "/twirp/rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI/List",
          { count: 1 }
        );
        return { healthy: r.status < 500, http_status: r.status, base_url: E2E_BASE };
      } catch (e) {
        return { healthy: false, error: e.message, base_url: E2E_BASE };
      }
    }

    case "e2e_list_testcases": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI/List",
        {
          count: args.count || 50,
          skip: args.skip || 0,
          service_name: args.service_name || "",
        }
      );
      return r.body;
    }

    case "e2e_get_testcase": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI/Get",
        {
          id: args.id || "",
          name: args.name || "",
          end_to_end_tests_branch_ref: args.branch_ref || "",
        }
      );
      return r.body;
    }

    case "e2e_create_testcase": {
      const priorityMap = { P0: 1, P1: 2, P2: 3 };
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.testcase.v1.TestcaseAPI/Create",
        {
          name: args.name,
          service_list: args.service_list,
          parent_service: args.parent_service || args.service_list[0],
          owner: args.owner || "saurav.k@razorpay.com",
          end_to_end_tests_branch_ref: args.branch_ref || "main",
          priority: priorityMap[args.priority || "P1"] || 2,
        }
      );
      return r.body;
    }

    case "e2e_run_testcase": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.test_execution.v1.TestExecutionAPI/Create",
        {
          testcase_id: args.testcase_id,
          execution_id: args.execution_id || "",
        }
      );
      return r.body;
    }

    case "e2e_get_execution": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.test_execution.v1.TestExecutionAPI/Get",
        { id: args.id }
      );
      return r.body;
    }

    case "e2e_get_execution_history": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.test_execution.v1.TestExecutionAPI/History",
        {
          count: args.count || 20,
          service_name: args.service_name || "",
        }
      );
      return r.body;
    }

    case "e2e_run_suite": {
      const r = await twirpPost(
        "/twirp/rzp.e2e_test_orchestrator.suite_execution.v1.SuiteExecutionAPI/Create",
        {
          name: args.name,
          testcase_ids: args.testcase_ids,
        }
      );
      return r.body;
    }

    case "e2e_run_roast": {
      const env = args.env || "test";
      const mode = args.mode || "intg";
      const timeout = (args.timeout_seconds || 600) * 1000;
      const envVars = [`-e ENV=${env}`, `-e MODE=${mode}`];
      if (args.include_groups) envVars.push(`-e INCLUDE_GROUPS=${args.include_groups}`);
      if (args.include_functions) envVars.push(`-e INCLUDE_FUNCTIONS=${args.include_functions}`);
      const cmd = `docker run --rm ${envVars.join(" ")} ${ROAST_IMAGE}`;

      return new Promise((resolve) => {
        const proc = exec(cmd, { timeout }, (error, stdout, stderr) => {
          if (error && error.killed) {
            resolve({ status: "timeout", error: `Timed out after ${args.timeout_seconds || 600}s`, cmd });
            return;
          }
          resolve({
            status: error ? "failed" : "passed",
            stdout: stdout.slice(0, 4000),
            stderr: stderr.slice(0, 2000),
            exit_code: error ? error.code : 0,
            cmd,
          });
        });
      });
    }

    case "e2e_detect_local_method": {
      const pyScript = `
import sys, json
sys.path.insert(0, '/Users/saurav.k/Projects/Agents/nemesis_v2')
from scripts.rubick_e2e import detect_local_e2e_method
services = json.loads(sys.argv[1])
result = {svc: detect_local_e2e_method(svc) for svc in services}
print(json.dumps(result))
`.trim();
      try {
        const out = execSync(
          `python3 -c "${pyScript.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}" '${JSON.stringify(args.services)}'`,
          { cwd: "/Users/saurav.k/Projects/Agents/nemesis_v2" }
        );
        return JSON.parse(out.toString().trim());
      } catch (e) {
        return { error: e.message };
      }
    }

    case "e2e_run_service_pipeline": {
      const pyScript = `
import sys, json
sys.path.insert(0, '/Users/saurav.k/Projects/Agents/nemesis_v2')
from scripts.rubick_e2e import run_service_pipeline, service_pipeline_report
from pathlib import Path
params = json.loads(sys.argv[1])
result = run_service_pipeline(
    feature_slug=params['feature_slug'],
    services=params['services'],
    env=params.get('env', 'e2e'),
    devstack_label=params.get('devstack_label', ''),
    timeout_per_service=params.get('timeout_per_service', 300),
)
report_md = service_pipeline_report(params['feature_slug'], result)
feat_dir = Path('workspace/features') / params['feature_slug']
feat_dir.mkdir(parents=True, exist_ok=True)
(feat_dir / 'e2e-report.md').write_text(report_md)
result['report_file'] = str(feat_dir / 'e2e-report.md')
print(json.dumps(result))
`.trim();
      try {
        const params = {
          feature_slug: args.feature_slug,
          services: args.services,
          env: args.env || "e2e",
          devstack_label: args.devstack_label || "",
          timeout_per_service: args.timeout_per_service || 300,
        };
        const out = execSync(
          `python3 -c "${pyScript.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}" '${JSON.stringify(params)}'`,
          { cwd: "/Users/saurav.k/Projects/Agents/nemesis_v2", timeout: (args.timeout_per_service || 300) * args.services.length * 1000 + 60000 }
        );
        return JSON.parse(out.toString().trim());
      } catch (e) {
        return { error: e.message, stderr: e.stderr?.toString() };
      }
    }

    case "e2e_run_local": {
      const pyScript = `
import sys, json
sys.path.insert(0, '/Users/saurav.k/Projects/Agents/nemesis_v2')
from scripts.rubick_e2e import run_local_e2e
params = json.loads(sys.argv[1])
result = run_local_e2e(
    service_slug=params['service'],
    env=params.get('env', 'e2e'),
    timeout=params.get('timeout', 300),
    devstack_label=params.get('devstack_label', ''),
)
print(json.dumps(result))
`.trim();
      try {
        const params = {
          service: args.service,
          env: args.env || "e2e",
          timeout: args.timeout || 300,
          devstack_label: args.devstack_label || "",
        };
        const out = execSync(
          `python3 -c "${pyScript.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}" '${JSON.stringify(params)}'`,
          { cwd: "/Users/saurav.k/Projects/Agents/nemesis_v2", timeout: (args.timeout || 300) * 1000 + 30000 }
        );
        return JSON.parse(out.toString().trim());
      } catch (e) {
        return { error: e.message };
      }
    }

    case "e2e_ingest_results": {
      const pyScript = `
import sys, json
sys.path.insert(0, '/Users/saurav.k/Projects/Agents/nemesis_v2')
from scripts.rubick_e2e import enrich_rubick_with_e2e, parse_e2e_results
params = json.loads(sys.argv[1])
results = parse_e2e_results(params)
enrich_rubick_with_e2e(params['feature_slug'], params['service'], results)
print(json.dumps({"ok": True, "status": results["status"]}))
`.trim();
      try {
        const params = {
          feature_slug: args.feature_slug,
          service: args.service,
          passed: args.passed || 0,
          failed: args.failed || 0,
          skipped: args.skipped || 0,
          duration_s: args.duration_s || 0,
          failures: args.failures || [],
        };
        const out = execSync(
          `python3 -c "${pyScript.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}" '${JSON.stringify(params)}'`,
          { cwd: "/Users/saurav.k/Projects/Agents/nemesis_v2" }
        );
        return JSON.parse(out.toString().trim());
      } catch (e) {
        return { ok: false, error: e.message };
      }
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── MCP Server setup ──────────────────────────────────────────────────────────

const server = new Server(
  { name: "e2e-orchestrator", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args } = req.params;
  try {
    const result = await handleTool(name, args || {});
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  } catch (e) {
    return {
      content: [{ type: "text", text: `Error: ${e.message}` }],
      isError: true,
    };
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
