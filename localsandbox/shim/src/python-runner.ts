#!/usr/bin/env -S deno run --allow-read --allow-write
/**
 * Isolated Python runner with minimal permissions.
 *
 * This script is spawned as a separate Deno subprocess with restricted
 * permissions to execute untrusted Python code safely.
 *
 * Input (via stdin): JSON with { fsRoot, code, cwd }
 * Output (via stdout): JSON with { stdout, stderr, exitCode, error? }
 */

import { loadPyodide, type PyodideInterface } from "npm:pyodide";

interface RunnerInput {
  fsRoot: string;
  code: string;
  cwd: string;
}

interface RunnerOutput {
  stdout: string;
  stderr: string;
  exitCode: number;
  error?: string;
}

let pyodide: PyodideInterface | null = null;
let capturedStdout = "";
let capturedStderr = "";

async function getPyodide(): Promise<PyodideInterface> {
  if (!pyodide) {
    pyodide = await loadPyodide({
      stdout: (msg) => {
        capturedStdout += msg + "\n";
      },
      stderr: (msg) => {
        capturedStderr += msg + "\n";
      },
    });
  }
  return pyodide;
}

async function runPython(input: RunnerInput): Promise<RunnerOutput> {
  // Reset captured output
  capturedStdout = "";
  capturedStderr = "";

  const py = await getPyodide();
  const mountPoint = "/data";

  // Ensure mount point exists
  try {
    py.FS.stat(mountPoint);
  } catch {
    py.FS.mkdir(mountPoint);
  }

  // Unmount if already mounted (from previous run)
  try {
    py.FS.unmount(mountPoint);
  } catch {
    // Not mounted, that's fine
  }

  // Mount the synced filesystem via NODEFS
  py.FS.mount(py.FS.filesystems.NODEFS, { root: input.fsRoot }, mountPoint);

  try {
    // Set working directory
    const pyCwd = input.cwd.startsWith("/")
      ? `${mountPoint}${input.cwd}`
      : `${mountPoint}/${input.cwd}`;

    py.globals.set("_localsandbox_cwd", pyCwd);
    py.globals.set("_localsandbox_mount_point", mountPoint);

    await py.runPythonAsync(`
import os
if os.path.exists(_localsandbox_cwd):
    os.chdir(_localsandbox_cwd)
else:
    os.chdir(_localsandbox_mount_point)
`);

    // Execute user code
    let exitCode = 0;
    let error: string | undefined;

    try {
      await py.runPythonAsync(input.code);
    } catch (e: unknown) {
      exitCode = 1;
      error = e instanceof Error ? e.message : String(e);
      // Ensure error is reported in stderr
      capturedStderr += error + "\n";
    }

    return { stdout: capturedStdout, stderr: capturedStderr, exitCode, error };
  } finally {
    // Unmount to allow cleanup
    try {
      py.FS.unmount(mountPoint);
    } catch {
      // Ignore unmount errors
    }
  }
}

// Main: read input from stdin, write output to stdout
async function main() {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();

  // Read all of stdin
  const chunks: Uint8Array[] = [];
  for await (const chunk of Deno.stdin.readable) {
    chunks.push(chunk);
  }
  const inputText = decoder.decode(
    chunks.reduce((acc, chunk) => {
      const merged = new Uint8Array(acc.length + chunk.length);
      merged.set(acc);
      merged.set(chunk, acc.length);
      return merged;
    }, new Uint8Array())
  );

  try {
    const input: RunnerInput = JSON.parse(inputText);
    const output = await runPython(input);
    await Deno.stdout.write(encoder.encode(JSON.stringify(output)));
  } catch (e: unknown) {
    const error: RunnerOutput = {
      stdout: "",
      stderr: "",
      exitCode: 1,
      error: e instanceof Error ? e.message : String(e),
    };
    await Deno.stdout.write(encoder.encode(JSON.stringify(error)));
  }
}

main();
