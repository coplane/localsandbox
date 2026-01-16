#!/usr/bin/env -S deno run --allow-read --allow-write --allow-env --allow-net --allow-ffi
/**
 * LocalSandbox TypeScript Shim CLI
 *
 * Bridges Python to just-bash + AgentFS.
 * All commands output JSON to stdout.
 */

import { Bash } from "npm:just-bash";
import { agentfs } from "npm:agentfs-sdk/just-bash";
import { AgentFS } from "npm:agentfs-sdk";
import { Buffer } from "node:buffer";
import process from "node:process";
import { parseArgs } from "node:util";
import { executePython } from "./python.ts";

interface BashResult {
  stdout: string;
  stderr: string;
  exitCode: number;
}

interface ErrorResult {
  error: string;
  type: string;
}

interface BinaryContent {
  base64: string;
}

type FileContent = string | BinaryContent;

function isBinaryContent(content: FileContent): content is BinaryContent {
  return typeof content === "object" && "base64" in content;
}

function output(data: BashResult | ErrorResult | unknown): void {
  console.log(JSON.stringify(data));
}

function outputError(error: unknown, type: string = "error"): void {
  const message = error instanceof Error ? error.message : String(error);
  output({ error: message, type });
}

async function bashCommand(
  dbPath: string,
  command: string,
  cwd: string,
  limits?: {
    maxLoopIterations?: number;
    maxCommandCount?: number;
  }
): Promise<void> {
  try {
    // Open AgentFS directly so we can close it properly
    const agent = await AgentFS.open({ path: dbPath });
    try {
      // To align with Python (which mounts at /data), we need to present
      // the AgentFS root at /data within the bash environment.
      // We can achieve this by creating a virtual root that contains 'data'.
      
      const fs = await agentfs(agent.fs, "/data");
      
      const bash = new Bash({
        fs,
        cwd,
        executionLimits: limits
          ? {
            maxLoopIterations: limits.maxLoopIterations,
            maxCommandCount: limits.maxCommandCount,
          }
          : undefined,
      });

      const startTime = Date.now();
      const result = await bash.exec(command);
      const endTime = Date.now();

      // Record the toolcall for history
      await agent.tools.record(
        "bash",
        startTime,
        endTime,
        { command, cwd },
        { exitCode: result.exitCode }
      );

      output({
        stdout: result.stdout,
        stderr: result.stderr,
        exitCode: result.exitCode,
      });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "execution_error");
    process.exit(1);
  }
}

async function seedCommand(
  dbPath: string,
  filesJson: string
): Promise<void> {
  try {
    const files = JSON.parse(filesJson) as Record<string, FileContent>;
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();
      const paths = Object.keys(files);

      // Write files directly using agent.fs
      for (const [path, content] of Object.entries(files)) {
        if (isBinaryContent(content)) {
          // Binary content - decode base64 and write as Buffer
          const decoded = Buffer.from(content.base64, "base64");
          await agent.fs.writeFile(path, decoded);
        } else {
          // Text content - write directly
          await agent.fs.writeFile(path, content, "utf8");
        }
      }

      const endTime = Date.now();

      // Record the seed operation
      await agent.tools.record(
        "seed",
        startTime,
        endTime,
        { paths, count: paths.length },
        { success: true }
      );

      output({ success: true, filesWritten: Object.keys(files).length });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "seed_error");
    process.exit(1);
  }
}

async function readFileCommand(dbPath: string, path: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();
      let content: string;
      let success = true;

      try {
        content = await agent.fs.readFile(path, "utf8");
      } catch (err) {
        const endTime = Date.now();
        await agent.tools.record(
          "read_file",
          startTime,
          endTime,
          { path },
          { success: false }
        );
        const message = err instanceof Error ? err.message : String(err);
        output({ error: message, type: "file_not_found" });
        process.exit(1);
      }

      const endTime = Date.now();
      await agent.tools.record(
        "read_file",
        startTime,
        endTime,
        { path },
        { success }
      );

      output({ content });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "read_error");
    process.exit(1);
  }
}

async function writeFileCommand(
  dbPath: string,
  path: string,
  content: string
): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();

      // writeFile auto-creates parent directories via ensureParentDirs
      await agent.fs.writeFile(path, content, "utf8");

      const endTime = Date.now();
      await agent.tools.record(
        "write_file",
        startTime,
        endTime,
        { path, contentLength: content.length },
        { success: true }
      );

      output({ success: true });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "write_error");
    process.exit(1);
  }
}

async function listFilesCommand(dbPath: string, path: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();
      let files: string[];

      try {
        files = await agent.fs.readdir(path);
      } catch (err) {
        const endTime = Date.now();
        await agent.tools.record(
          "list_files",
          startTime,
          endTime,
          { path },
          { success: false }
        );
        const message = err instanceof Error ? err.message : String(err);
        output({ error: message, type: "list_error" });
        process.exit(1);
      }

      const endTime = Date.now();
      await agent.tools.record(
        "list_files",
        startTime,
        endTime,
        { path },
        { success: true, count: files.length }
      );

      output({ files });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "list_error");
    process.exit(1);
  }
}

async function existsCommand(dbPath: string, path: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();
      let exists = false;

      try {
        await agent.fs.stat(path);
        exists = true;
      } catch {
        exists = false;
      }

      const endTime = Date.now();
      await agent.tools.record(
        "exists",
        startTime,
        endTime,
        { path },
        { exists }
      );

      output({ exists });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "exists_error");
    process.exit(1);
  }
}

async function deleteFileCommand(dbPath: string, path: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const startTime = Date.now();

      try {
        await agent.fs.unlink(path);
      } catch (err) {
        const endTime = Date.now();
        await agent.tools.record(
          "delete_file",
          startTime,
          endTime,
          { path },
          { success: false }
        );
        const message = err instanceof Error ? err.message : String(err);
        output({ error: message, type: "delete_error" });
        process.exit(1);
      }

      const endTime = Date.now();
      await agent.tools.record(
        "delete_file",
        startTime,
        endTime,
        { path },
        { success: true }
      );

      output({ success: true });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "delete_error");
    process.exit(1);
  }
}

async function kvGetCommand(dbPath: string, key: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const value = await agent.kv.get<string>(key);
      output({ value: value ?? null });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "kv_get_error");
    process.exit(1);
  }
}

async function kvSetCommand(
  dbPath: string,
  key: string,
  value: string
): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      await agent.kv.set(key, value);
      output({ success: true });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "kv_set_error");
    process.exit(1);
  }
}

async function kvDeleteCommand(dbPath: string, key: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      await agent.kv.delete(key);
      output({ success: true });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "kv_delete_error");
    process.exit(1);
  }
}

async function kvKeysCommand(dbPath: string, prefix: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      const items = await agent.kv.list(prefix);
      const keys = items.map((item) => item.key);
      output({ keys });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "kv_keys_error");
    process.exit(1);
  }
}

async function checkpointCommand(dbPath: string): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      // Force a WAL checkpoint to merge WAL into main database
      const db = agent.getDatabase();
      await db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
      output({ success: true });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "checkpoint_error");
    process.exit(1);
  }
}

async function historyCommand(dbPath: string, limit: number): Promise<void> {
  try {
    const agent = await AgentFS.open({ path: dbPath });
    try {
      // Get all recent tool calls (since timestamp 0 = all)
      const entries = await agent.tools.getRecent(0, limit);
      output({ entries });
    } finally {
      await agent.close();
    }
  } catch (error) {
    outputError(error, "history_error");
    process.exit(1);
  }
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.error("Usage: localsandbox-shim <command> [options]");
    console.error(
      "Commands: bash, execute-python, seed, read-file, write-file, list-files, exists, delete-file, kv-get, kv-set, kv-delete, kv-keys, checkpoint, history"
    );
    process.exit(1);
  }

  const command = args[0];

  switch (command) {
    case "bash": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          command: { type: "string" },
          cwd: { type: "string", default: "/home/user" },
          limits: { type: "string" },
        },
      });

      if (!values.db || !values.command) {
        console.error("bash requires --db and --command");
        process.exit(1);
      }

      const limits = values.limits ? JSON.parse(values.limits) : undefined;
      await bashCommand(values.db, values.command, values.cwd!, limits);
      break;
    }

    case "execute-python": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          code: { type: "string" },
          cwd: { type: "string", default: "/home/user" },
        },
      });

      if (!values.db || !values.code) {
        console.error("execute-python requires --db and --code");
        process.exit(1);
      }

      try {
        const result = await executePython(values.db, values.code, values.cwd!);
        output(result);
      } catch (error) {
        outputError(error, "python_error");
        process.exit(1);
      }
      break;
    }

    case "seed": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          files: { type: "string" },
        },
      });

      if (!values.db || !values.files) {
        console.error("seed requires --db and --files");
        process.exit(1);
      }

      await seedCommand(values.db, values.files);
      break;
    }

    case "read-file": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          path: { type: "string" },
        },
      });

      if (!values.db || !values.path) {
        console.error("read-file requires --db and --path");
        process.exit(1);
      }

      await readFileCommand(values.db, values.path);
      break;
    }

    case "write-file": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          path: { type: "string" },
          content: { type: "string" },
        },
      });

      if (!values.db || !values.path || values.content === undefined) {
        console.error("write-file requires --db, --path, and --content");
        process.exit(1);
      }

      await writeFileCommand(values.db, values.path, values.content);
      break;
    }

    case "list-files": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          path: { type: "string" },
        },
      });

      if (!values.db || !values.path) {
        console.error("list-files requires --db and --path");
        process.exit(1);
      }

      await listFilesCommand(values.db, values.path);
      break;
    }

    case "exists": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          path: { type: "string" },
        },
      });

      if (!values.db || !values.path) {
        console.error("exists requires --db and --path");
        process.exit(1);
      }

      await existsCommand(values.db, values.path);
      break;
    }

    case "delete-file": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          path: { type: "string" },
        },
      });

      if (!values.db || !values.path) {
        console.error("delete-file requires --db and --path");
        process.exit(1);
      }

      await deleteFileCommand(values.db, values.path);
      break;
    }

    case "kv-get": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          key: { type: "string" },
        },
      });

      if (!values.db || !values.key) {
        console.error("kv-get requires --db and --key");
        process.exit(1);
      }

      await kvGetCommand(values.db, values.key);
      break;
    }

    case "kv-set": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          key: { type: "string" },
          value: { type: "string" },
        },
      });

      if (!values.db || !values.key || values.value === undefined) {
        console.error("kv-set requires --db, --key, and --value");
        process.exit(1);
      }

      await kvSetCommand(values.db, values.key, values.value);
      break;
    }

    case "kv-delete": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          key: { type: "string" },
        },
      });

      if (!values.db || !values.key) {
        console.error("kv-delete requires --db and --key");
        process.exit(1);
      }

      await kvDeleteCommand(values.db, values.key);
      break;
    }

    case "kv-keys": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          prefix: { type: "string", default: "" },
        },
      });

      if (!values.db) {
        console.error("kv-keys requires --db");
        process.exit(1);
      }

      await kvKeysCommand(values.db, values.prefix!);
      break;
    }

    case "checkpoint": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
        },
      });

      if (!values.db) {
        console.error("checkpoint requires --db");
        process.exit(1);
      }

      await checkpointCommand(values.db);
      break;
    }

    case "history": {
      const { values } = parseArgs({
        args: args.slice(1),
        options: {
          db: { type: "string" },
          limit: { type: "string", default: "100" },
        },
      });

      if (!values.db) {
        console.error("history requires --db");
        process.exit(1);
      }

      await historyCommand(values.db, parseInt(values.limit!, 10));
      break;
    }

    default:
      console.error(`Unknown command: ${command}`);
      process.exit(1);
  }
}

main().catch((error) => {
  outputError(error, "fatal_error");
  process.exit(1);
});
