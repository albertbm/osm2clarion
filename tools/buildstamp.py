#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Content-addressed cache for a build made of ordered steps, plus the manifest
that records what actually ran. Nothing here names a project: a step is just a
name, a list of input files, an opaque args value, a list of (git repo, tool
files) pairs, and the files it writes.

A step is a dict with:
    name            unique within the build
    inputs          list of file paths read
    args            any JSON-serialisable value (flags, parameters) that changes
                     the step's output if it changes
    tool_groups     [(repo_path_or_None, [tool_file, ...]), ...] - repo_path is a
                     git worktree; its HEAD and a diff scoped to those files are
                     part of the stamp, so an uncommitted edit to a tool this step
                     doesn't use does not invalidate it
    output_files    every file the step writes, including ones it writes as a
                     side effect of running later steps' inputs through it
    mutates         names of earlier steps in the same build whose output_files
                     this step also rewrites in place (optional)

decide() and run_steps() work from this dict; they do not run commands
themselves - the caller supplies run_cmds(step), since how a step is actually
invoked (cwd, subprocess shape, logging) is the caller's business.
"""
import hashlib
import json
import os
import subprocess
import time


def sha(b):
    return hashlib.sha256(b).hexdigest()


def sha_file(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def file_sha_map(paths):
    return {p: sha_file(p) for p in paths if os.path.exists(p)}


def git_head(repo):
    try:
        return subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'],
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def git_dirty(repo):
    try:
        out = subprocess.run(['git', '-C', repo, 'status', '--porcelain'],
                              capture_output=True, text=True, check=True).stdout
        return bool(out.strip())
    except (OSError, subprocess.CalledProcessError):
        return False


def git_diff(repo, paths=None):
    """(sha256, text) of `git diff HEAD`, scoped to paths if given. Empty/'' if not a repo."""
    cmd = ['git', '-C', repo, 'diff', 'HEAD', '--']
    if paths:
        cmd += [os.path.relpath(p, repo) for p in paths]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        out = ''
    return sha(out.encode()), out


def repo_state(repo, paths=None):
    """None if repo isn't a git worktree, else {head, diff_sha256} scoped to paths."""
    head = git_head(repo)
    if head is None:
        return None
    diff_sha, _ = git_diff(repo, paths)
    return dict(head=head, diff_sha256=diff_sha)


def tool_state(tool_groups):
    out = []
    for repo, files in tool_groups:
        files = sorted(files)
        out.append(dict(repo=repo, state=repo_state(repo, files) if repo else None,
                         files=file_sha_map(files)))
    return out


def input_state(step):
    return dict(inputs=file_sha_map(sorted(step.get('inputs', []))),
                args=sha(json.dumps(step.get('args'), sort_keys=True).encode()),
                tools=tool_state(step.get('tool_groups', [])))


def output_state(step):
    return file_sha_map(sorted(step.get('output_files', [])))


def stamp_path(build_dir, name):
    return os.path.join(build_dir, '.stamp-%s.json' % name)


def load_stamp(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_stamp(build_dir, step, cur_in):
    stamp = dict(input=cur_in, output=output_state(step))
    with open(stamp_path(build_dir, step['name']), 'w') as f:
        json.dump(stamp, f, indent=1)
    return stamp


def refresh_output(build_dir, name, output_files):
    """A later step just rewrote an earlier one's files in place: fix its recorded
    output hashes (not its input record) so the earlier step still reuses next time."""
    path = stamp_path(build_dir, name)
    stamp = load_stamp(path)
    if stamp is None:
        return
    stamp['output'] = file_sha_map(sorted(output_files))
    with open(path, 'w') as f:
        json.dump(stamp, f, indent=1)


def input_diff_reason(prior, cur):
    if prior.get('args') != cur['args']:
        return 'args changed'
    if prior.get('tools') != cur['tools']:
        return 'a tool file or its upstream repo state changed'
    if prior.get('inputs') != cur['inputs']:
        return 'an input file changed'
    return None


def decide(build_dir, step, reuse, force):
    """-> (should_run, label, reason, cur_in). label is 'ran' | 'reused' | 'rerun'."""
    cur_in = input_state(step)
    prior = load_stamp(stamp_path(build_dir, step['name'])) if reuse else None
    if prior is None:
        return True, 'ran', None, cur_in   # nothing to reuse, forced or not
    if force:
        return True, 'rerun', 'an earlier step in this build reran', cur_in
    reason = input_diff_reason(prior.get('input', {}), cur_in)
    if reason:
        return True, 'rerun', reason, cur_in
    missing = [p for p in sorted(step.get('output_files', [])) if not os.path.exists(p)]
    if missing:
        return True, 'rerun', 'missing output: %s' % missing[0], cur_in
    if output_state(step) != prior.get('output', {}):
        return True, 'rerun', 'output on disk no longer matches its stamp', cur_in
    return False, 'reused', None, cur_in


def run_steps(build_dir, step_list, reuse, run_cmds, on_line=print):
    """Decide and, if needed, run each step in order. A rerun anywhere forces every
    later step to rerun too, since a step may read an earlier one's output file by
    content without that showing in its own listed inputs."""
    by_name = {s['name']: s for s in step_list}
    force = False
    for step in step_list:
        should_run, label, reason, cur_in = decide(build_dir, step, reuse, force)
        tag = label if reason is None else '%s: %s' % (label, reason)
        on_line('step %-6s: %s' % (step['name'], tag))
        if not should_run:
            step['ran'] = 'reused'
            continue
        force = True
        t0 = time.time()
        run_cmds(step)
        step['ran'] = '%.0f s' % (time.time() - t0)
        # re-hash inputs after running: a step that extracts its own input files (e.g.
        # off a base image) may not have had them on disk yet when cur_in was computed
        write_stamp(build_dir, step, input_state(step))
        for other in step.get('mutates', []):
            refresh_output(build_dir, other, by_name[other].get('output_files', []))
    return step_list


def append_post_step(manifest_path, name, args, image_path):
    """Record one post-build step (run after the card exists) in an existing manifest."""
    with open(manifest_path) as f:
        m = json.load(f)
    m.setdefault('post_steps', []).append(
        dict(name=name, args=args, image_sha256=sha_file(image_path)))
    with open(manifest_path, 'w') as f:
        json.dump(m, f, indent=1, ensure_ascii=False)


def repo_manifest_state(repo):
    head = git_head(repo)
    if head is None:
        return dict(head=None, dirty=False)
    return dict(head=head, dirty=git_dirty(repo))


def save_diff_if_dirty(repo, dest_path):
    """Write `git diff HEAD` to dest_path if repo has uncommitted changes. Returns
    (dirty, diff_sha256) either way; dest_path is only created when dirty."""
    dirty = git_dirty(repo)
    diff_sha, text = git_diff(repo)
    if dirty:
        with open(dest_path, 'w') as f:
            f.write(text)
    return dirty, diff_sha


if __name__ == '__main__':
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == 'post-step':
        # post-step <manifest.json> <name> <args text> <image>
        _, _, manifest_path, name, args, image_path = sys.argv
        append_post_step(manifest_path, name, args, image_path)
        print(sha_file(image_path), manifest_path)
    else:
        raise SystemExit('usage: buildstamp.py post-step <manifest.json> <name> <args> <image>')
