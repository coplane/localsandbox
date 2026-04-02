#!/usr/bin/env -S deno run --allow-read --allow-write
/**
 * Isolated Python runner with minimal permissions.
 *
 * Communicates via line-delimited JSON envelopes over stdin/stdout.
 * The Pyodide runtime persists across multiple executions within a session.
 */

import { loadPyodide, type PyodideInterface } from "pyodide";

import type {
  CompleteEnvelope,
  ExecuteEnvelope,
  FatalErrorEnvelope,
  RunnerStartEnvelope,
  ToolCallEnvelope,
  ToolErrorEnvelope,
  ToolManifestEntry,
  ToolResultEnvelope,
} from "./bridge-types.ts";
import { type BM25Retriever, index as indexBm25 } from "./bm25.ts";

interface RunnerInput {
  fsRoot: string;
  code: string;
  cwd: string;
  preloadPackages?: string[];
}

type ToolResponseEnvelope = ToolResultEnvelope | ToolErrorEnvelope;

const ERROR_SENTINEL_KEY = "__error__";

let pyodide: PyodideInterface | null = null;
let capturedStdout = "";
let capturedStderr = "";
const loadedPackages = new Set<string>();
const encoder = new TextEncoder();

type ToolSearchDetail = "brief" | "full";

function formatSearchResult(
  manifest: ToolManifestEntry,
  score: number,
  detail: ToolSearchDetail,
): Record<string, unknown> {
  const result: Record<string, unknown> = {
    name: manifest.name,
    description: manifest.description ?? "",
    score: Number(score.toFixed(4)),
  };

  if (detail === "full") {
    result.input_schema = manifest.input_schema ?? null;
    result.output_schema = manifest.output_schema ?? null;
    result.timeout_ms = manifest.timeout_ms ?? null;
  }

  return result;
}

function searchTools(
  retriever: BM25Retriever<ToolManifestEntry>,
  query: string,
  detail: ToolSearchDetail,
  limit: number,
): Array<Record<string, unknown>> {
  return retriever.search(query, limit, { minScoreRatio: 0.15 }).map((entry) =>
    formatSearchResult(entry.document, entry.score, detail)
  );
}

// Prevent unhandled promise rejections (e.g. from async Python/JS interop)
// from crashing the Deno process. Inspired by DSPy's runner.js approach.
globalThis.addEventListener("unhandledrejection", (event) => {
  event.preventDefault();
  capturedStderr += `Unhandled async error: ${
    (event.reason as Error)?.message ?? event.reason
  }\n`;
});

class LineReader {
  private reader: ReadableStreamDefaultReader<Uint8Array>;
  private decoder = new TextDecoder();
  private buffer = "";

  constructor(stream: ReadableStream<Uint8Array>) {
    this.reader = stream.getReader();
  }

  async readLine(): Promise<string | null> {
    while (true) {
      const newlineIndex = this.buffer.indexOf("\n");
      if (newlineIndex >= 0) {
        const line = this.buffer.slice(0, newlineIndex);
        this.buffer = this.buffer.slice(newlineIndex + 1);
        return line.replace(/\r$/, "");
      }

      const { value, done } = await this.reader.read();
      if (done) {
        if (this.buffer.length === 0) {
          return null;
        }
        const line = this.buffer;
        this.buffer = "";
        return line.replace(/\r$/, "");
      }
      this.buffer += this.decoder.decode(value, { stream: true });
    }
  }
}

class ToolCallSession {
  private nextToolCallId = 1;

  constructor(private lineReader: LineReader) {}

  async callTool(name: string, payload: unknown): Promise<unknown> {
    const id = `t${this.nextToolCallId++}`;
    await writeEnvelope(
      {
        type: "tool_call",
        id,
        name,
        payload,
      } satisfies ToolCallEnvelope,
    );

    const responseLine = await this.lineReader.readLine();
    if (responseLine === null) {
      throw new Error("Stdin closed while waiting for tool result");
    }

    let response: ToolResponseEnvelope;
    try {
      response = JSON.parse(responseLine) as ToolResponseEnvelope;
    } catch (error) {
      throw new Error(
        `Invalid tool response: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    }

    if (response.id !== id) {
      throw new Error(
        `Response id mismatch: expected ${id}, got ${response.id}`,
      );
    }

    if (response.type === "tool_result") {
      return response.payload;
    }

    throw new Error(`[${response.error_type}] ${response.message}`);
  }
}

async function writeEnvelope(
  envelope: ToolCallEnvelope | CompleteEnvelope | FatalErrorEnvelope,
): Promise<void> {
  await Deno.stdout.write(encoder.encode(JSON.stringify(envelope) + "\n"));
}

async function getPyodide(): Promise<PyodideInterface> {
  if (!pyodide) {
    pyodide = await loadPyodide({
      stdout: (msg: string) => capturedStdout += msg + "\n",
      stderr: (msg: string) => capturedStderr += msg + "\n",
    });
  }
  return pyodide;
}

async function preloadPackages(
  py: PyodideInterface,
  packages: string[],
): Promise<void> {
  for (const pkg of packages) {
    if (loadedPackages.has(pkg)) {
      continue;
    }
    await py.loadPackage(pkg, { messageCallback: () => {} });
    loadedPackages.add(pkg);
  }
}

const MOUNT_POINT = "/data";

async function updateCwd(
  py: PyodideInterface,
  cwd: string,
): Promise<void> {
  let normalizedCwd = cwd;
  if (normalizedCwd.startsWith(MOUNT_POINT + "/")) {
    normalizedCwd = normalizedCwd.slice(MOUNT_POINT.length);
  } else if (normalizedCwd === MOUNT_POINT) {
    normalizedCwd = "/";
  }

  const pyCwd = normalizedCwd.startsWith("/")
    ? `${MOUNT_POINT}${normalizedCwd}`
    : `${MOUNT_POINT}/${normalizedCwd}`;

  py.globals.set("_localsandbox_cwd", pyCwd);
  py.globals.set("_localsandbox_mount_point", MOUNT_POINT);
  await py.runPythonAsync(`
import os
if os.path.exists(_localsandbox_cwd):
    os.chdir(_localsandbox_cwd)
else:
    os.chdir(_localsandbox_mount_point)
`);
}

async function preparePyodide(
  input: RunnerInput,
): Promise<PyodideInterface> {
  capturedStdout = "";
  capturedStderr = "";

  const py = await getPyodide();
  const preloadPackagesList = input.preloadPackages ?? [];
  if (preloadPackagesList.length > 0) {
    await preloadPackages(py, preloadPackagesList);
  }

  try {
    py.FS.stat(MOUNT_POINT);
  } catch {
    py.FS.mkdir(MOUNT_POINT);
  }

  try {
    py.FS.unmount(MOUNT_POINT);
  } catch {
    // Already unmounted.
  }

  py.FS.mount(py.FS.filesystems.NODEFS, { root: input.fsRoot }, MOUNT_POINT);

  await updateCwd(py, input.cwd);
  return py;
}

async function executeAndReport(
  py: PyodideInterface,
  code: string,
): Promise<void> {
  capturedStdout = "";
  capturedStderr = "";

  let exitCode = 0;
  let error: string | undefined;
  try {
    await py.runPythonAsync(code);
  } catch (e: unknown) {
    exitCode = 1;
    error = e instanceof Error ? e.message : String(e);
    capturedStderr += error + "\n";
  }

  await writeEnvelope({
    type: "complete",
    stdout: capturedStdout,
    stderr: capturedStderr,
    exit_code: exitCode,
    error,
  });
}

async function runSession(
  start: RunnerStartEnvelope,
  lineReader: LineReader,
): Promise<void> {
  const py = await preparePyodide({
    fsRoot: start.fs_root,
    code: start.code,
    cwd: start.cwd,
    preloadPackages: start.preload_packages,
  });

  const toolSession = new ToolCallSession(lineReader);
  const toolSearchRetriever = indexBm25(start.tools ?? [], {
    fields: [
      {
        extractText: (tool) => tool.name,
        weight: 3.5,
      },
      {
        extractText: (tool) => tool.description ?? "",
        weight: 1.5,
      },
      {
        extractText: (tool) => {
          const inputSchema = tool.input_schema == null
            ? ""
            : JSON.stringify(tool.input_schema);
          const outputSchema = tool.output_schema == null
            ? ""
            : JSON.stringify(tool.output_schema);
          return `${inputSchema} ${outputSchema}`;
        },
        weight: 0.35,
      },
    ],
  });

  // Return structured error objects instead of throwing from JS so that
  // unhandled rejections don't crash Deno. The Python wrapper detects the
  // sentinel key and raises a proper exception.
  py.registerJsModule("_host_tools_js", {
    call: async (name: unknown, payload: unknown) => {
      if (typeof name !== "string") {
        return {
          [ERROR_SENTINEL_KEY]: true,
          message: "host_tools.call expects a string tool name",
        };
      }
      try {
        return await toolSession.callTool(name, payload);
      } catch (error) {
        return {
          [ERROR_SENTINEL_KEY]: true,
          message: error instanceof Error ? error.message : String(error),
        };
      }
    },
    search: async (
      query: unknown,
      detail: unknown,
      limit: unknown,
    ) => {
      if (typeof query !== "string") {
        return {
          [ERROR_SENTINEL_KEY]: true,
          message: "host_tools.search expects a string query",
        };
      }
      const searchDetail = detail === "full" ? "full" : "brief";
      const searchLimit = typeof limit === "number" ? limit : 10;
      return await Promise.resolve(
        searchTools(toolSearchRetriever, query, searchDetail, searchLimit),
      );
    },
    error_key: ERROR_SENTINEL_KEY,
  });

  await py.runPythonAsync(`
import sys
import types
from _host_tools_js import call as _js_call, search as _js_search, error_key as _error_key
from pyodide.ffi import JsProxy, run_sync

_ERROR_SENTINEL_KEY = _error_key

def _unwrap_result(result):
    if isinstance(result, JsProxy):
        result = result.to_py()
    if isinstance(result, dict) and result.get(_ERROR_SENTINEL_KEY):
        raise RuntimeError(result.get("message", "Tool call error"))
    return result

def call(name, payload):
    return _unwrap_result(run_sync(_js_call(name, payload)))

def search(query, detail="brief", limit=10):
    if not isinstance(query, str):
        raise TypeError("host_tools.search expects a string query")
    if detail not in ("brief", "full"):
        raise ValueError("host_tools.search detail must be 'brief' or 'full'")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("host_tools.search limit must be an integer")
    if limit < 1:
        raise ValueError("host_tools.search limit must be at least 1")
    return _unwrap_result(run_sync(_js_search(query, detail, limit)))

host_tools = types.ModuleType("host_tools")
host_tools.call = call
host_tools.search = search
sys.modules["host_tools"] = host_tools
  `);

  try {
    await executeAndReport(py, start.code);

    while (true) {
      const nextLine = await lineReader.readLine();
      if (nextLine === null) break;

      const next = JSON.parse(nextLine) as ExecuteEnvelope;
      if (next.type !== "execute") {
        throw new Error(`Expected execute envelope, got ${next.type}`);
      }

      const pkgs = next.preload_packages ?? [];
      if (pkgs.length > 0) {
        await preloadPackages(py, pkgs);
      }
      await updateCwd(py, next.cwd);
      await executeAndReport(py, next.code);
    }
  } finally {
    try {
      py.FS.unmount(MOUNT_POINT);
    } catch {
      // Ignore unmount errors.
    }
  }
}

async function main(): Promise<void> {
  const lineReader = new LineReader(Deno.stdin.readable);
  const startLine = await lineReader.readLine();
  if (startLine === null) {
    await writeEnvelope({
      type: "fatal_error",
      message: "Start envelope was not provided",
    });
    return;
  }

  try {
    const start = JSON.parse(startLine) as RunnerStartEnvelope;
    if (start.type !== "start") {
      throw new Error(`Expected start envelope, got ${start.type}`);
    }
    await runSession(start, lineReader);
  } catch (error) {
    await writeEnvelope({
      type: "fatal_error",
      message: error instanceof Error ? error.message : String(error),
    });
  }
}

main();
