#!/usr/bin/env python
import glob, os, stat, subprocess, sys, tempfile
from bup import bloom, git, midx, options, vfs
from bup.helpers import handle_ctrl_c, log, progress, qprogress, saved_errors
from os.path import basename

optspec = """
bup gc [options...]
--
v,verbose   increase log output (can be used more than once)
threshold   only rewrite a packfile if it's over this percent garbage [10]
#,compress= set compression level to # (0-9, 9 is highest) [1]
"""


class Nonlocal:
    pass


# FIXME: unify with the walk_object() version in bup-get
def walk_object(cat_pipe, id, verbose=None, parent_path=[],
                stop_at=None,
                include_data=None):
    # Yield everything reachable from id via cat_pipe, stopping
    # whenever stop_at(id) returns true.  Produce (id, type data) for
    # each item, or (id, type) if include_data is false.
    if stop_at and stop_at(id):
        return

    item_it = cat_pipe.get(id)  # FIXME: use include_data
    type = item_it.next()

    # FIXME: remove once cat pipe supports include_data.
    need_data = include_data or type in ('commit', 'tree')
    if not need_data:
        list(item_it) # Dump the data (iterator).

    if type == 'blob':
        yield include_data and (id, type, ''.join(item_it)) or (id, type)
    elif type == 'commit':
        data = ''.join(item_it)
        yield include_data and (id, type, data) or (id, type)

        commit_items = git.parse_commit(data)
        tree_id = commit_items.tree
        for x in walk_object(cat_pipe, tree_id, verbose, parent_path,
                             stop_at, include_data):
            yield x
        parents = commit_items.parents
        for pid in parents:
            for x in walk_object(cat_pipe, pid, verbose, parent_path,
                                 stop_at, include_data):
                yield x
    elif type == 'tree':
        data = ''.join(item_it)
        yield include_data and (id, type, data) or (id, type)
        for (mode, name, ent_id) in git.tree_decode(data):
            if not verbose > 1:
                for x in walk_object(cat_pipe, ent_id.encode('hex'),
                                     verbose, parent_path,
                                     stop_at, include_data):
                    yield x
            else:
                demangled, bup_type = git.demangle_name(name)
                sub_path = parent_path + [demangled]
                # Don't print the sub-parts of chunked files.
                sub_v = verbose if bup_type == git.BUP_NORMAL else None
                for x in walk_object(cat_pipe, ent_id.encode('hex'),
                                     sub_v, sub_path,
                                     stop_at, include_data):
                    yield x
                if stat.S_ISDIR(mode):
                    if verbose > 1 and bup_type == git.BUP_NORMAL:
                        log('%s/\n' % '/'.join(sub_path))
                    elif verbose > 2:  # (and BUP_CHUNKED)
                        log('%s\n' % '/'.join(sub_path))
                elif verbose > 2:
                    log('%s\n' % '/'.join(sub_path))
    else:
        raise Exception('unexpected repository object type %r' % type)


def count_objects(dir):
    # For now we'll just use open_idx(), but we could probably be much
    # more efficient since all we need is a single integer (the last
    # fanout entry) from each index.
    object_count = 0
    for idx_name in glob.glob(os.path.join(dir, '*.idx')):
        idx = git.open_idx(idx_name)
        object_count += len(idx)
    return object_count


def find_live_objects(existing_count, cat_pipe, opt):
    pack_dir = git.repo('objects/pack')
    live_objs = set()
    stop_at = lambda (x): x in live_objs
    for ref_name, ref_id in git.list_refs():
        for id, type in walk_object(cat_pipe, ref_id.encode('hex'), opt.verbose,
                                    parent_path=[ref_name],
                                    stop_at=stop_at,
                                    include_data=None):
            live_objs.add(id.decode('hex'))
    return live_objs


def sweep(live_objects, existing_count, cat_pipe, opt):
    # Traverse all the packs, saving the (probably) live data.

    ns = Nonlocal()
    ns.stale_files = []
    def remove_stale_files(new_pack_prefix):
        if opt.verbose and new_pack_prefix:
            log('created ' + basename(new_pack_prefix) + '\n')
        for p in ns.stale_files:
            if opt.verbose:
                log('gc: removing ' + basename(p) + '\n')
            os.unlink(p)
        ns.stale_files = []

    writer = git.PackWriter(objcache_maker=None,
                            compression_level=opt.compress,
                            run_midx=False,
                            on_pack_finish=remove_stale_files)

    # FIXME: sanity check .idx names vs .pack names?
    collect_count = 0
    for idx_name in glob.glob(os.path.join(git.repo('objects/pack'), '*.idx')):
        if opt.verbose:
            qprogress('gc: preserving live data (%d%% complete)\r'
                      % ((float(collect_count) / existing_count) * 100))
        idx = git.open_idx(idx_name)

        idx_live_count = 0
        for i in xrange(0, len(idx)):
            sha = idx.shatable[i * 20 : (i + 1) * 20]
            if sha in live_objects:
                idx_live_count += 1

        collect_count += idx_live_count
        if idx_live_count == 0:
            if opt.verbose:
                log('gc: %s (delete)\n'
                    % git.repo_rel(basename(idx_name)))
            ns.stale_files.append(idx_name)
            ns.stale_files.append(idx_name[:-3] + 'pack')
            continue

        live_frac = idx_live_count / float(len(idx))
        if live_frac > ((100 - opt.threshold) / 100.0):
            if opt.verbose:
                log('gc: %s (keep: %d%% live)\n' % (git.repo_rel(basename(idx_name)),
                                                     live_frac * 100))
            continue

        if opt.verbose:
            log('gc: %s (rewrite: %.2f%% live)\n' % (basename(idx_name),
                                                      live_frac * 100))
        for i in xrange(0, len(idx)):
            sha = idx.shatable[i * 20 : (i + 1) * 20]
            if sha in live_objects:
                item_it = cat_pipe.get(sha.encode('hex'))
                type = item_it.next()
                writer.write(sha, type, ''.join(item_it))

        ns.stale_files.append(idx_name)
        ns.stale_files.append(idx_name[:-3] + 'pack')

    if opt.verbose:
        progress('gc: preserving live data (%d%% complete)\n'
                 % ((float(collect_count) / existing_count) * 100))

    # Nothing should have recreated midx/bloom yet.
    pack_dir = git.repo('objects/pack')
    assert(not os.path.exists(os.path.join(pack_dir, 'bup.bloom')))
    assert(not glob.glob(os.path.join(pack_dir, '*.midx')))

    # try/catch should call writer.abort()?
    # This will finally run midx.
    writer.close()  # Can only change refs (if needed) after this.
    remove_stale_files(None)  # In case we didn't write to the writer.

    if opt.verbose:
        log('gc: discarded %d%% of objects\n'
            % ((existing_count - count_objects(pack_dir))
               / float(existing_count) * 100))


# FIXME: server mode?
# FIXME: make sure client handles server-side changes reasonably
# FIXME: fdatasync new packs in packwriter?

handle_ctrl_c()

o = options.Options(optspec)
(opt, flags, extra) = o.parse(sys.argv[1:])

if extra:
    o.fatal('no positional parameters expected')

if opt.threshold:
    try:
        opt.threshold = int(opt.threshold)
    except ValueError:
        o.fatal('threshold must be an integer percentage value')
    if opt.threshold < 0 or opt.threshold > 100:
        o.fatal('threshold must be an integer percentage value')

git.check_repo_or_die()

cat_pipe = vfs.cp()
existing_count = count_objects(git.repo('objects/pack'))
if opt.verbose:
    log('gc: found %d objects\n' % existing_count)
if not existing_count:
    if opt.verbose:
        log('gc: nothing to collect\n')
else:
    live_objects = find_live_objects(existing_count, cat_pipe, opt)
    # FIXME: just rename midxes and bloom, and restore them at the end if
    # we didn't change any packs?
    if opt.verbose: log('gc: clearing midx files\n')
    midx.clear_midxes()
    if opt.verbose: log('gc: clearing bloom filter\n')
    bloom.clear_bloom(git.repo('objects/pack'))
    if opt.verbose: log('gc: clearing reflog\n')
    expirelog_cmd = ['git', 'reflog', 'expire', '--all']
    expirelog = subprocess.Popen(expirelog_cmd, preexec_fn = git._gitenv())
    git._git_wait(' '.join(expirelog_cmd), expirelog)
    if opt.verbose: log('gc: removing unreachable data\n')
    sweep(live_objects, existing_count, cat_pipe, opt)
    del live_objects

if saved_errors:
    log('WARNING: %d errors encountered during gc\n' % len(saved_errors))
    sys.exit(1)
