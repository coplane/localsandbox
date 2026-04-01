#!/usr/bin/env -S deno run --allow-read --allow-write --allow-env --allow-net --allow-ffi --allow-run
/**
 * Persistent NDJSON server for localsandbox.
 *
 * One instance per LocalSandbox lifetime. Handles all operations (bash, file
 * I/O, KV, history, Python execution) via JSON envelopes over stdin/stdout.
 * The AgentFS database stays open for the process lifetime.
 */

import { Bash } from "just-bash";
import { agentfs } from "agentfs-sdk/just-bash";
import { AgentFS } from "agentfs-sdk";
import { Buffer } from "node:buffer";
import { type ChildProcess, spawn } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import process from "node:process";
import { createInterface, type Interface } from "node:readline";
import { fileURLToPath } from "node:url";

import type {
  CompleteEnvelope,
  FatalErrorEnvelope,
  RunnerStartEnvelope,
  ServerRequest,
  ToolCallEnvelope,
  ToolErrorEnvelope,
  ToolResultEnvelope,
} from "./bridge-types.ts";

// ============================================================================
// Utilities
// ============================================================================

const DATA_PREFIX = "/data";

function normalizePathToAgentFS(userPath: string): string {
  if (userPath.startsWith(DATA_PREFIX + "/")) {
    return userPath.slice(DATA_PREFIX.length);
  }
  if (userPath === DATA_PREFIX) {
    return "/";
  }
  return userPath;
}

class LineReader {
  private lines: Array<string | null> = [];
  private waiters: Array<(line: string | null) => void> = [];

  constructor(private readonly rl: Interface) {
    rl.on("line", (line) => {
      const waiter = this.waiters.shift();
      if (waiter) {
        waiter(line);
      } else {
        this.lines.push(line);
      }
    });
    rl.on("close", () => {
      while (this.waiters.length > 0) {
        const waiter = this.waiters.shift();
        waiter?.(null);
      }
      this.lines.push(null);
    });
  }

  async readLine(): Promise<string | null> {
    if (this.lines.length > 0) {
      return this.lines.shift() ?? null;
    }
    return await new Promise((resolve) => {
      this.waiters.push(resolve);
    });
  }
}

async function writeJsonLine(
  stream: NodeJS.WritableStream,
  value: unknown,
): Promise<void> {
  const line = JSON.stringify(value) + "\n";
  await new Promise<void>((resolve, reject) => {
    const ok = stream.write(line, (error?: Error | null) => {
      if (error) reject(error);
      else resolve();
    });
    if (!ok) stream.once("drain", resolve);
  });
}

function respond(id: string, data: unknown): Promise<void> {
  return writeJsonLine(process.stdout, { id, type: "result", data });
}

function respondError(
  id: string,
  error: string,
  errorType: string,
): Promise<void> {
  return writeJsonLine(process.stdout, {
    id,
    type: "error",
    error,
    error_type: errorType,
  });
}

function truncateText(value: unknown, maxLength = 200): string {
  const serialized = typeof value === "string" ? value : JSON.stringify(value);
  if (serialized.length <= maxLength) return serialized;
  return serialized.slice(0, maxLength) + "...";
}

type BinaryContent = { base64: string };
type FileContent = string | BinaryContent;

function isBinaryContent(content: FileContent): content is BinaryContent {
  return typeof content === "object" && "base64" in content;
}

// ============================================================================
// Handlers
// ============================================================================

async function handleBash(
  agent: AgentFS,
  req: ServerRequest & { type: "bash" },
): Promise<void> {
  const bashFs = await agentfs(agent.fs, "/data");
  const bash = new Bash({
    fs: bashFs,
    cwd: req.cwd,
    executionLimits: req.limits,
  });

  const startTime = Date.now();
  const result = await bash.exec(req.command);
  const endTime = Date.now();

  await agent.tools.record(
    "bash",
    startTime,
    endTime,
    { command: req.command, cwd: req.cwd },
    { exitCode: result.exitCode },
  );

  await respond(req.id, {
    stdout: result.stdout,
    stderr: result.stderr,
    exitCode: result.exitCode,
  });
}

async function handleSeed(
  agent: AgentFS,
  req: ServerRequest & { type: "seed" },
): Promise<void> {
  const startTime = Date.now();
  const paths = Object.keys(req.files);

  for (const [filePath, content] of Object.entries(req.files)) {
    const agentPath = normalizePathToAgentFS(filePath);
    if (isBinaryContent(content)) {
      await agent.fs.writeFile(
        agentPath,
        Buffer.from(content.base64, "base64"),
      );
    } else {
      await agent.fs.writeFile(agentPath, content, "utf8");
    }
  }

  const endTime = Date.now();
  await agent.tools.record(
    "seed",
    startTime,
    endTime,
    { paths, count: paths.length },
    { success: true },
  );

  await respond(req.id, { success: true, filesWritten: paths.length });
}

async function handleReadFile(
  agent: AgentFS,
  req: ServerRequest & { type: "read_file" },
): Promise<void> {
  const startTime = Date.now();
  const agentPath = normalizePathToAgentFS(req.path);

  try {
    if (req.binary) {
      const data = await agent.fs.readFile(agentPath);
      const endTime = Date.now();
      await agent.tools.record("read_file", startTime, endTime, {
        path: req.path,
        binary: true,
      }, { success: true });
      await respond(req.id, {
        content: data.toString("base64"),
        encoding: "base64",
      });
    } else {
      const content = await agent.fs.readFile(agentPath, "utf8");
      const endTime = Date.now();
      await agent.tools.record("read_file", startTime, endTime, {
        path: req.path,
      }, { success: true });
      await respond(req.id, { content });
    }
  } catch (err) {
    const endTime = Date.now();
    await agent.tools.record("read_file", startTime, endTime, {
      path: req.path,
    }, { success: false });
    await respondError(
      req.id,
      err instanceof Error ? err.message : String(err),
      "file_not_found",
    );
  }
}

async function handleWriteFile(
  agent: AgentFS,
  req: ServerRequest & { type: "write_file" },
): Promise<void> {
  const startTime = Date.now();
  const agentPath = normalizePathToAgentFS(req.path);

  if (req.binary) {
    const data = Buffer.from(req.content, "base64");
    await agent.fs.writeFile(agentPath, data);
    const endTime = Date.now();
    await agent.tools.record("write_file", startTime, endTime, {
      path: req.path,
      contentLength: data.length,
      binary: true,
    }, { success: true });
  } else {
    await agent.fs.writeFile(agentPath, req.content, "utf8");
    const endTime = Date.now();
    await agent.tools.record("write_file", startTime, endTime, {
      path: req.path,
      contentLength: req.content.length,
    }, { success: true });
  }

  await respond(req.id, { success: true });
}

async function handleListFiles(
  agent: AgentFS,
  req: ServerRequest & { type: "list_files" },
): Promise<void> {
  const startTime = Date.now();
  const agentPath = normalizePathToAgentFS(req.path);

  try {
    const files = await agent.fs.readdir(agentPath);
    const endTime = Date.now();
    await agent.tools.record("list_files", startTime, endTime, {
      path: req.path,
    }, { success: true, count: files.length });
    await respond(req.id, { files });
  } catch (err) {
    const endTime = Date.now();
    await agent.tools.record("list_files", startTime, endTime, {
      path: req.path,
    }, { success: false });
    await respondError(
      req.id,
      err instanceof Error ? err.message : String(err),
      "file_not_found",
    );
  }
}

async function handleExists(
  agent: AgentFS,
  req: ServerRequest & { type: "exists" },
): Promise<void> {
  const startTime = Date.now();
  const agentPath = normalizePathToAgentFS(req.path);

  let exists = false;
  try {
    await agent.fs.stat(agentPath);
    exists = true;
  } catch {
    exists = false;
  }

  const endTime = Date.now();
  await agent.tools.record("exists", startTime, endTime, { path: req.path }, {
    exists,
  });
  await respond(req.id, { exists });
}

async function handleDeleteFile(
  agent: AgentFS,
  req: ServerRequest & { type: "delete_file" },
): Promise<void> {
  const startTime = Date.now();
  const agentPath = normalizePathToAgentFS(req.path);

  try {
    await agent.fs.unlink(agentPath);
    const endTime = Date.now();
    await agent.tools.record("delete_file", startTime, endTime, {
      path: req.path,
    }, { success: true });
    await respond(req.id, { success: true });
  } catch (err) {
    const endTime = Date.now();
    await agent.tools.record("delete_file", startTime, endTime, {
      path: req.path,
    }, { success: false });
    await respondError(
      req.id,
      err instanceof Error ? err.message : String(err),
      "file_not_found",
    );
  }
}

async function handleKVGet(
  agent: AgentFS,
  req: ServerRequest & { type: "kv_get" },
): Promise<void> {
  const value = await agent.kv.get<string>(req.key);
  await respond(req.id, { value: value ?? null });
}

async function handleKVSet(
  agent: AgentFS,
  req: ServerRequest & { type: "kv_set" },
): Promise<void> {
  await agent.kv.set(req.key, req.value);
  await respond(req.id, { success: true });
}

async function handleKVDelete(
  agent: AgentFS,
  req: ServerRequest & { type: "kv_delete" },
): Promise<void> {
  await agent.kv.delete(req.key);
  await respond(req.id, { success: true });
}

async function handleKVKeys(
  agent: AgentFS,
  req: ServerRequest & { type: "kv_keys" },
): Promise<void> {
  const items = await agent.kv.list(req.prefix);
  const keys = items.map((item: { key: string }) => item.key);
  await respond(req.id, { keys });
}

async function handleCheckpoint(
  agent: AgentFS,
  req: ServerRequest & { type: "checkpoint" },
): Promise<void> {
  const db = agent.getDatabase();
  await db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
  await respond(req.id, { success: true });
}

async function handleHistory(
  agent: AgentFS,
  req: ServerRequest & { type: "history" },
): Promise<void> {
  const entries = await agent.tools.getRecent(0, req.limit);
  await respond(req.id, { entries });
}

// ============================================================================
// Python execution
// ============================================================================

function getEnvVar(name: string): string | undefined {
  try {
    return Deno.env.get(name) ?? undefined;
  } catch {
    return undefined;
  }
}

function getDenoCacheDir(): string | undefined {
  const denoDir = getEnvVar("DENO_DIR");
  if (denoDir) return denoDir;

  const home = getEnvVar("HOME") ?? os.homedir();
  if (!home) return undefined;

  const xdgCache = getEnvVar("XDG_CACHE_HOME");
  if (xdgCache) return path.join(xdgCache, "deno");

  if (os.platform() === "darwin") {
    return path.join(home, "Library", "Caches", "deno");
  }
  if (os.platform() === "win32") {
    const localAppData = getEnvVar("LOCALAPPDATA");
    if (localAppData) return path.join(localAppData, "deno");
    return path.join(home, "AppData", "Local", "deno");
  }
  return path.join(home, ".cache", "deno");
}

function expandAllowedPath(entry: string): string[] {
  const expanded = [entry];
  try {
    const resolved = fs.realpathSync(entry);
    if (resolved !== entry) expanded.push(resolved);
  } catch {
    // Keep original entry if realpath fails.
  }
  return expanded;
}

function getFsRootAllowList(fsRoot: string): string[] {
  return Array.from(new Set(expandAllowedPath(path.resolve(fsRoot))));
}

function getRunnerPath(): string {
  const currentFile = fileURLToPath(import.meta.url);
  return path.join(path.dirname(currentFile), "python-runner.ts");
}

function getRunnerReadAllowList(fsRoot: string, runnerPath: string): string[] {
  const runnerDir = path.dirname(runnerPath);
  const shimDir = path.dirname(runnerDir);
  const denoCacheDir = getDenoCacheDir();
  const allowList = [
    ...getFsRootAllowList(fsRoot),
    path.resolve(runnerDir),
    path.join(shimDir, "node_modules"),
    path.join(shimDir, "deno.json"),
    denoCacheDir,
  ].filter((entry): entry is string => Boolean(entry));

  return Array.from(
    new Set(
      allowList.flatMap((entry) => expandAllowedPath(path.resolve(entry))),
    ),
  );
}

function getRunnerWriteAllowList(
  fsRoot: string,
  runnerPath: string,
): string[] {
  const runnerDir = path.dirname(runnerPath);
  const shimDir = path.dirname(runnerDir);
  const allowList = [
    ...getFsRootAllowList(fsRoot),
    path.join(shimDir, "node_modules"),
  ];

  return Array.from(
    new Set(
      allowList.flatMap((entry) => expandAllowedPath(path.resolve(entry))),
    ),
  );
}

function spawnRunner(
  fsRoot: string,
  preloadPackages?: string[],
): ChildProcess {
  const runnerPath = getRunnerPath();
  const readAllowArg = `--allow-read=${
    getRunnerReadAllowList(fsRoot, runnerPath).join(",")
  }`;
  const writeAllowArg = `--allow-write=${
    getRunnerWriteAllowList(fsRoot, runnerPath).join(",")
  }`;
  const allowNetArg = preloadPackages && preloadPackages.length > 0
    ? "--allow-net=cdn.jsdelivr.net"
    : null;

  return spawn(
    "deno",
    [
      "run",
      readAllowArg,
      writeAllowArg,
      "--allow-env=HOME,DENO_DIR,XDG_CACHE_HOME",
      ...(allowNetArg ? [allowNetArg] : []),
      "--no-prompt",
      runnerPath,
    ],
    {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: path.dirname(path.dirname(runnerPath)),
    },
  );
}

function isFuseAvailable(): boolean {
  return os.platform() === "linux";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForMount(
  mountPoint: string,
  timeoutMs = 5000,
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const entries = fs.readdirSync(mountPoint);
      if (entries.length > 0) return true;
    } catch {
      // Not ready yet.
    }
    await sleep(100);
  }
  return false;
}

async function listAllFiles(
  agentFs: AgentFS["fs"],
  dir: string,
): Promise<string[]> {
  const results: string[] = [];
  try {
    const entries = await agentFs.readdirPlus(dir);
    for (const entry of entries) {
      const fullPath = dir === "/" ? `/${entry.name}` : `${dir}/${entry.name}`;
      if (entry.stats.isDirectory()) {
        results.push(...(await listAllFiles(agentFs, fullPath)));
      } else {
        results.push(fullPath);
      }
    }
  } catch {
    // Directory doesn't exist or is empty.
  }
  return results;
}

async function syncAgentFSToDir(
  agentFs: AgentFS["fs"],
  targetDir: string,
): Promise<void> {
  const agentFiles = new Set(await listAllFiles(agentFs, "/"));
  const localFiles = listLocalFiles(targetDir);

  for (const filePath of localFiles) {
    if (!agentFiles.has(filePath)) {
      fs.rmSync(path.join(targetDir, filePath), { force: true });
    }
  }

  for (const filePath of agentFiles) {
    const localPath = path.join(targetDir, filePath);
    fs.mkdirSync(path.dirname(localPath), { recursive: true });
    const content = await agentFs.readFile(filePath);
    fs.writeFileSync(localPath, content);
  }
}

function listLocalFiles(dir: string, prefix = ""): string[] {
  const results: string[] = [];
  try {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const fullPath = prefix ? `${prefix}/${entry.name}` : `/${entry.name}`;
      const localPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        results.push(...listLocalFiles(localPath, fullPath));
      } else {
        results.push(fullPath);
      }
    }
  } catch {
    // Directory doesn't exist.
  }
  return results;
}

async function syncDirToAgentFS(
  sourceDir: string,
  agentFs: AgentFS["fs"],
): Promise<void> {
  const localFiles = new Set(listLocalFiles(sourceDir));
  const agentFiles = await listAllFiles(agentFs, "/");

  for (const filePath of agentFiles) {
    if (!localFiles.has(filePath)) {
      await agentFs.unlink(filePath);
    }
  }

  for (const filePath of localFiles) {
    const localPath = path.join(sourceDir, filePath);
    const content = fs.readFileSync(localPath);
    await agentFs.writeFile(filePath, content);
  }
}

function cleanupTempDir(tempDir: string): void {
  try {
    fs.rmSync(tempDir, { recursive: true, force: true });
  } catch {
    // Best-effort cleanup.
  }
}

function collectStderr(stream: NodeJS.ReadableStream): {
  lines: string[];
  done: Promise<void>;
} {
  const lines: string[] = [];
  const rl = createInterface({ input: stream, crlfDelay: Infinity });
  const done = new Promise<void>((resolve) => {
    rl.on("line", (line) => lines.push(line));
    rl.on("close", () => resolve());
  });
  return { lines, done };
}

type RunnerEnvelope = ToolCallEnvelope | CompleteEnvelope | FatalErrorEnvelope;
type SdkToolResponse = ToolResultEnvelope | ToolErrorEnvelope;

/**
 * Manages a persistent Python runner subprocess. Handles FUSE/sync
 * filesystem strategy and tool call relay.
 */
class PythonSession {
  private proc: ChildProcess | null = null;
  private runnerReader: LineReader | null = null;
  private runnerStdout: Interface | null = null;
  private stderrCapture: { lines: string[]; done: Promise<void> } | null = null;
  private fsRoot: string | null = null;
  private tempDir: string | null = null;
  private mountProcess: ChildProcess | null = null;
  private currentExecutionKey: string | null = null;
  private sessionId = crypto.randomUUID();

  constructor(
    private readonly agent: AgentFS,
    private readonly dbPath: string,
  ) {}

  async execute(
    req: ServerRequest & { type: "execute_python" },
    sdkReader: LineReader,
  ): Promise<void> {
    const executionKey = this.getExecutionKey(req);
    const needsRestart = this.proc === null || this.proc.exitCode !== null ||
      this.currentExecutionKey !== executionKey;

    if (needsRestart) {
      await this.stop();
      this.sessionId = crypto.randomUUID();
      await this.setupFs();
      this.startRunner(req);
      this.currentExecutionKey = executionKey;
    } else {
      if (this.tempDir && !this.mountProcess) {
        await syncAgentFSToDir(this.agent.fs, this.tempDir);
      }
      await this.sendExecuteEnvelope(req);
    }

    await this.relayExecution(req, sdkReader);
  }

  private getExecutionKey(
    req: ServerRequest & { type: "execute_python" },
  ): string {
    const preloadPackages = [...(req.preload_packages ?? [])].sort();
    return JSON.stringify({
      tools: req.tools ?? [],
      preloadPackages,
    });
  }

  private async setupFs(): Promise<void> {
    if (isFuseAvailable()) {
      try {
        this.tempDir = fs.mkdtempSync(
          path.join(os.tmpdir(), "localsandbox-python-"),
        );
        const mountPoint = path.join(this.tempDir, "mnt");
        fs.mkdirSync(mountPoint);

        this.mountProcess = spawn("agentfs", [
          "mount",
          "-f",
          this.dbPath,
          mountPoint,
        ], {
          stdio: ["ignore", "pipe", "pipe"],
        });

        const mountErrorPromise = new Promise<never>((_, reject) => {
          this.mountProcess?.once("error", (err) => reject(err));
        });

        const mounted = await Promise.race([
          waitForMount(mountPoint),
          mountErrorPromise,
        ]);
        if (!mounted) throw new Error("Failed to mount AgentFS via FUSE");

        this.fsRoot = mountPoint;
        return;
      } catch (fuseError) {
        console.error("FUSE mount failed, falling back to sync:", fuseError);
        this.cleanupMount();
      }
    }

    // Sync mode fallback.
    this.tempDir = fs.mkdtempSync(
      path.join(os.tmpdir(), "localsandbox-python-"),
    );
    this.fsRoot = this.tempDir;
    await syncAgentFSToDir(this.agent.fs, this.tempDir);
  }

  private startRunner(req: ServerRequest & { type: "execute_python" }): void {
    if (!this.fsRoot) throw new Error("Filesystem not set up");

    this.proc = spawnRunner(this.fsRoot, req.preload_packages);

    if (!this.proc.stdin || !this.proc.stdout || !this.proc.stderr) {
      this.proc.kill("SIGTERM");
      throw new Error("Failed to open runner subprocess pipes");
    }

    this.runnerStdout = createInterface({
      input: this.proc.stdout,
      crlfDelay: Infinity,
    });
    this.runnerReader = new LineReader(this.runnerStdout);
    this.stderrCapture = collectStderr(this.proc.stderr);

    const envelope: RunnerStartEnvelope = {
      type: "start",
      fs_root: this.fsRoot,
      code: req.code,
      cwd: req.cwd,
      preload_packages: req.preload_packages ?? [],
      tools: req.tools ?? [],
    };

    this.proc.stdin.write(JSON.stringify(envelope) + "\n");
  }

  private async sendExecuteEnvelope(
    req: ServerRequest & { type: "execute_python" },
  ): Promise<void> {
    if (!this.proc?.stdin) throw new Error("Runner not running");

    await writeJsonLine(this.proc.stdin, {
      type: "execute",
      code: req.code,
      cwd: req.cwd,
      preload_packages: req.preload_packages,
    });
  }

  private async relayExecution(
    req: ServerRequest & { type: "execute_python" },
    sdkReader: LineReader,
  ): Promise<void> {
    if (!this.proc?.stdin || !this.runnerReader || !this.stderrCapture) {
      throw new Error("Runner not running");
    }

    const pythonStartTime = Date.now();
    let toolCallCount = 0;

    while (true) {
      const line = await this.runnerReader.readLine();
      if (line === null) {
        const stderr = this.stderrCapture.lines.join("\n");
        await this.stop();
        throw new Error(`Runner exited before completion: ${stderr}`);
      }

      let envelope: RunnerEnvelope;
      try {
        envelope = JSON.parse(line) as RunnerEnvelope;
      } catch {
        this.stderrCapture.lines.push(line);
        continue;
      }

      if (envelope.type === "tool_call") {
        toolCallCount += 1;
        const toolStartedAt = Date.now();

        // Forward tool call to SDK.
        await writeJsonLine(process.stdout, {
          id: envelope.id,
          request_id: req.id,
          type: "tool_call",
          name: envelope.name,
          payload: envelope.payload,
        });

        // Read tool result from SDK.
        const responseLine = await sdkReader.readLine();
        if (responseLine === null) {
          throw new Error("SDK closed stdin while waiting for tool result");
        }

        const toolResponse = JSON.parse(responseLine) as SdkToolResponse;
        const toolCompletedAt = Date.now();

        await this.agent.tools.record(
          "python_tool_call",
          toolStartedAt,
          toolCompletedAt,
          {
            sessionId: this.sessionId,
            toolName: envelope.name,
            requestPreview: truncateText(envelope.payload),
          },
          toolResponse.type === "tool_result"
            ? {
              success: true,
              responsePreview: truncateText(toolResponse.payload),
            }
            : { success: false, errorType: toolResponse.error_type },
        );

        await writeJsonLine(this.proc.stdin!, toolResponse);
        continue;
      }

      if (envelope.type === "fatal_error") {
        await this.stop();
        throw new Error(envelope.message);
      }

      if (envelope.type === "complete") {
        const pythonCompletedAt = Date.now();
        await this.agent.tools.record(
          "python",
          pythonStartTime,
          pythonCompletedAt,
          {
            codeLength: req.code.length,
            cwd: req.cwd,
            preloadPackages: (req.preload_packages ?? []).join(","),
            sessionId: this.sessionId,
            toolCallCount,
          },
          { exitCode: envelope.exit_code },
        );

        // Sync back in sync mode.
        if (this.tempDir && !this.mountProcess) {
          await syncDirToAgentFS(this.tempDir, this.agent.fs);
        }

        await respond(req.id, {
          stdout: envelope.stdout,
          stderr: envelope.stderr,
          exit_code: envelope.exit_code,
          error: envelope.error,
        });
        return;
      }
    }
  }

  private cleanupMount(): void {
    if (this.mountProcess) {
      this.mountProcess.kill("SIGTERM");
      this.mountProcess = null;
    }
  }

  async stop(): Promise<void> {
    if (this.proc) {
      try {
        if (this.proc.stdin) this.proc.stdin.end();
      } catch { /* ignore */ }
      if (this.proc.exitCode === null) {
        this.proc.kill("SIGTERM");
      }
      if (this.runnerStdout) this.runnerStdout.close();
      if (this.stderrCapture) await this.stderrCapture.done;
      this.proc = null;
      this.runnerReader = null;
      this.runnerStdout = null;
      this.stderrCapture = null;
    }

    this.cleanupMount();

    if (this.tempDir) {
      cleanupTempDir(this.tempDir);
      this.tempDir = null;
      this.fsRoot = null;
    }

    this.currentExecutionKey = null;
  }
}

// ============================================================================
// Main
// ============================================================================

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  const dbIdx = args.indexOf("--db");
  if (dbIdx === -1 || dbIdx + 1 >= args.length) {
    console.error("Usage: server.ts --db <path>");
    process.exit(1);
  }
  const dbPath = args[dbIdx + 1];

  const agent = await AgentFS.open({ path: dbPath });
  const sdkReader = new LineReader(
    createInterface({ input: process.stdin, crlfDelay: Infinity }),
  );
  const pythonSession = new PythonSession(agent, dbPath);

  // Signal that the server is ready.
  await writeJsonLine(process.stdout, { type: "ready" });

  try {
    while (true) {
      const line = await sdkReader.readLine();
      if (line === null) break;

      let req: ServerRequest;
      try {
        req = JSON.parse(line) as ServerRequest;
      } catch {
        await writeJsonLine(process.stdout, {
          type: "fatal_error",
          message: `Invalid JSON: ${line.slice(0, 100)}`,
        });
        continue;
      }

      try {
        switch (req.type) {
          case "bash":
            await handleBash(agent, req);
            break;
          case "seed":
            await handleSeed(agent, req);
            break;
          case "read_file":
            await handleReadFile(agent, req);
            break;
          case "write_file":
            await handleWriteFile(agent, req);
            break;
          case "list_files":
            await handleListFiles(agent, req);
            break;
          case "exists":
            await handleExists(agent, req);
            break;
          case "delete_file":
            await handleDeleteFile(agent, req);
            break;
          case "kv_get":
            await handleKVGet(agent, req);
            break;
          case "kv_set":
            await handleKVSet(agent, req);
            break;
          case "kv_delete":
            await handleKVDelete(agent, req);
            break;
          case "kv_keys":
            await handleKVKeys(agent, req);
            break;
          case "checkpoint":
            await handleCheckpoint(agent, req);
            break;
          case "history":
            await handleHistory(agent, req);
            break;
          case "execute_python":
            await pythonSession.execute(req, sdkReader);
            break;
          case "shutdown":
            await respond(req.id, { success: true });
            await pythonSession.stop();
            await agent.close();
            return;
          default:
            await respondError(
              (req as { id: string }).id,
              `Unknown request type: ${(req as { type: string }).type}`,
              "unknown_command",
            );
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        try {
          await respondError(req.id, message, "internal_error");
        } catch {
          // If we can't write the response, the pipe is broken.
          break;
        }
      }
    }
  } finally {
    await pythonSession.stop();
    await agent.close();
  }
}

main();
