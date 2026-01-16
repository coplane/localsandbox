/**
 * Python execution via Pyodide in an isolated subprocess
 *
 * Security model:
 * - Main shim process: has full permissions, handles AgentFS sync
 * - Python runner subprocess: minimal permissions (read/write temp dir + runner deps/cache, no network)
 *
 * Two approaches:
 * 1. FUSE mount (Linux only) - mount AgentFS directly, zero sync overhead
 * 2. Sync (fallback) - sync files to temp dir, run Python in isolated subprocess
 */

import { AgentFS } from "npm:agentfs-sdk";
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { fileURLToPath } from "node:url";

export interface PythonResult {
  stdout: string;
  stderr: string;
  exitCode: number;
  error?: string;
}

/**
 * Check if FUSE mount is available (Linux only)
 */
function isFuseAvailable(): boolean {
  return os.platform() === "linux";
}

/**
 * Get path to the python-runner.ts script
 */
function getRunnerPath(): string {
  const currentFile = fileURLToPath(import.meta.url);
  return path.join(path.dirname(currentFile), "python-runner.ts");
}

function getEnvVar(name: string): string | undefined {
  try {
    return Deno.env.get(name) ?? undefined;
  } catch {
    return undefined;
  }
}

function getDenoCacheDir(): string | undefined {
  const denoDir = getEnvVar("DENO_DIR");
  if (denoDir) {
    return denoDir;
  }

  const home = getEnvVar("HOME") ?? os.homedir();
  if (!home) {
    return undefined;
  }

  const xdgCache = getEnvVar("XDG_CACHE_HOME");
  if (xdgCache) {
    return path.join(xdgCache, "deno");
  }

  if (os.platform() === "darwin") {
    return path.join(home, "Library", "Caches", "deno");
  }

  if (os.platform() === "win32") {
    const localAppData = getEnvVar("LOCALAPPDATA");
    if (localAppData) {
      return path.join(localAppData, "deno");
    }
    return path.join(home, "AppData", "Local", "deno");
  }

  return path.join(home, ".cache", "deno");
}

function expandAllowedPath(entry: string): string[] {
  const expanded = [entry];
  try {
    const resolved = fs.realpathSync(entry);
    if (resolved !== entry) {
      expanded.push(resolved);
    }
  } catch {
    // Keep original entry if realpath fails.
  }
  return expanded;
}

function getFsRootAllowList(fsRoot: string): string[] {
  return Array.from(
    new Set(expandAllowedPath(path.resolve(fsRoot)))
  );
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
      allowList.flatMap((entry) => expandAllowedPath(path.resolve(entry)))
    )
  );
}

/**
 * Execute Python code in an isolated subprocess with minimal permissions
 */
async function runPythonIsolated(
  fsRoot: string,
  code: string,
  cwd: string
): Promise<PythonResult> {
  const runnerPath = getRunnerPath();
  const readAllowList = getRunnerReadAllowList(fsRoot, runnerPath);
  const readAllowArg = `--allow-read=${readAllowList.join(",")}`;
  const writeAllowList = getFsRootAllowList(fsRoot);
  const writeAllowArg = `--allow-write=${writeAllowList.join(",")}`;

  // Spawn python-runner with restricted permissions:
  // - Allow read for temp dir, runner deps, and Deno cache
  // - Allow write only to the specific temp directory
  // - No network access
  // - No FFI
  // - No environment access (except HOME for Deno cache location)
  const proc = spawn(
    "deno",
    [
      "run",
      readAllowArg,
      writeAllowArg,
      "--allow-env=HOME,DENO_DIR,XDG_CACHE_HOME",
      "--no-prompt",
      runnerPath,
    ],
    {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: path.dirname(path.dirname(runnerPath)),
    }
  );

  return new Promise((resolve, reject) => {
    const input = JSON.stringify({ fsRoot, code, cwd });
    let stdout = "";
    let stderr = "";

    proc.stdout?.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr?.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("error", (err) => {
      reject(new Error(`Failed to spawn Python runner: ${err.message}`));
    });

    proc.on("close", (exitCode) => {
      try {
        // Parse the JSON result from stdout
        const result = JSON.parse(stdout) as PythonResult;
        resolve(result);
      } catch {
        // If we can't parse the output, return an error
        resolve({
          stdout: "",
          stderr: stderr || stdout,
          exitCode: exitCode ?? 1,
          error: `Python runner failed: ${stderr || stdout}`,
        });
      }
    });

    // Send input and close stdin
    proc.stdin?.write(input);
    proc.stdin?.end();
  });
}

/**
 * Execute Python code using FUSE mount approach (Linux)
 */
async function executePythonWithFuse(
  dbPath: string,
  code: string,
  cwd: string
): Promise<PythonResult> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "localsandbox-python-"));
  const mountPoint = path.join(tempDir, "mnt");
  fs.mkdirSync(mountPoint);

  let mountProcess: ChildProcess | null = null;

  try {
    // Start agentfs mount in foreground mode
    mountProcess = spawn("agentfs", ["mount", "-f", dbPath, mountPoint], {
      stdio: ["ignore", "pipe", "pipe"],
    });

    // Wait for mount to be ready
    const mounted = await waitForMount(mountPoint);
    if (!mounted) {
      throw new Error("Failed to mount AgentFS via FUSE");
    }

    // Run Python in isolated subprocess with the mounted filesystem
    return await runPythonIsolated(mountPoint, code, cwd);
  } finally {
    // Cleanup: kill mount process
    if (mountProcess) {
      mountProcess.kill("SIGTERM");
    }

    // Remove temp directory
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {
      // Ignore cleanup errors
    }
  }
}

/**
 * Execute Python code using sync approach (fallback)
 */
async function executePythonWithSync(
  agent: AgentFS,
  code: string,
  cwd: string
): Promise<PythonResult> {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "localsandbox-python-"));

  try {
    // Sync AgentFS to temp directory
    await syncAgentFSToDir(agent.fs, tempDir);

    // Run Python in isolated subprocess with the synced filesystem
    const result = await runPythonIsolated(tempDir, code, cwd);

    // Sync changes back to AgentFS
    await syncDirToAgentFS(tempDir, agent.fs);

    return result;
  } finally {
    // Cleanup temp directory
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {
      // Ignore cleanup errors
    }
  }
}

/**
 * Wait for a mount point to become available
 */
async function waitForMount(
  mountPoint: string,
  timeoutMs = 5000
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const entries = fs.readdirSync(mountPoint);
      if (entries.length > 0) {
        return true;
      }
    } catch {
      // Not ready yet
    }
    await sleep(100);
  }
  return false;
}

/**
 * Recursively list all files in AgentFS
 */
async function listAllFiles(
  agentFs: AgentFS["fs"],
  dir: string
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
    // Directory doesn't exist or is empty
  }

  return results;
}

/**
 * Sync AgentFS to a local directory
 */
async function syncAgentFSToDir(
  agentFs: AgentFS["fs"],
  targetDir: string
): Promise<void> {
  const files = await listAllFiles(agentFs, "/");

  for (const filePath of files) {
    const localPath = path.join(targetDir, filePath);
    const dir = path.dirname(localPath);

    // Create parent directories
    fs.mkdirSync(dir, { recursive: true });

    // Read from AgentFS, write to local
    const content = await agentFs.readFile(filePath);
    fs.writeFileSync(localPath, content);
  }
}

/**
 * Recursively list files in a local directory
 */
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
    // Directory doesn't exist
  }

  return results;
}

/**
 * Sync a local directory back to AgentFS
 */
async function syncDirToAgentFS(
  sourceDir: string,
  agentFs: AgentFS["fs"]
): Promise<void> {
  const localFiles = listLocalFiles(sourceDir);

  for (const filePath of localFiles) {
    const localPath = path.join(sourceDir, filePath);
    const content = fs.readFileSync(localPath);
    await agentFs.writeFile(filePath, content);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Main entry point for Python execution
 */
export async function executePython(
  dbPath: string,
  code: string,
  cwd: string
): Promise<PythonResult> {
  const agent = await AgentFS.open({ path: dbPath });
  const startTime = Date.now();

  try {
    let result: PythonResult;

    if (isFuseAvailable()) {
      // Try FUSE approach on Linux
      try {
        result = await executePythonWithFuse(dbPath, code, cwd);
      } catch (fuseError) {
        // Fall back to sync approach if FUSE fails
        console.error("FUSE mount failed, falling back to sync:", fuseError);
        result = await executePythonWithSync(agent, code, cwd);
      }
    } else {
      // Use sync approach on non-Linux platforms
      result = await executePythonWithSync(agent, code, cwd);
    }

    const endTime = Date.now();

    // Record tool call
    await agent.tools.record(
      "python",
      startTime,
      endTime,
      { codeLength: code.length, cwd },
      { exitCode: result.exitCode }
    );

    return result;
  } finally {
    await agent.close();
  }
}
