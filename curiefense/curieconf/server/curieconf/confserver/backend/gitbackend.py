import os
from io import BytesIO
import git, gitdb
from . import Backends, CurieBackend, CurieBackendException
import urllib
import time
import threading
import json
from flask import abort
from curieconf import utils
import jmespath
import fasteners
from typing import Dict, List


CURIE_AUTHOR = git.Actor("Curiefense API", "curiefense@reblaze.com")

INTERNAL_PREFIX = "_internal_"

BRANCH_BASE = INTERNAL_PREFIX+"base"
BRANCH_DB   = INTERNAL_PREFIX+"db"

class CurieGitBackendException(CurieBackendException):
    pass

def is_internal(branchname):
    return branchname.startswith(INTERNAL_PREFIX)

def commit(index, msg):
    index.commit(msg,
                 author=CURIE_AUTHOR,
                 committer=CURIE_AUTHOR)


def add_file(repo, fname, content):
    istream = gitdb.IStream("blob", len(content), BytesIO(content))
    repo.odb.store(istream)
    blob = git.Blob(repo, istream.binsha, 0o100644, fname)
    repo.index.add([git.IndexEntry.from_blob(blob)])

def get_repo(pth):
    os.makedirs(pth, exist_ok=True)
    try:
        repo = git.Repo(pth)
        if BRANCH_BASE not in repo.heads:
            raise Exception("Repository at [%s] does not have a branch [%s]" % (pth, BRANCH_BASE))
        if BRANCH_DB not in repo.heads:
            raise Exception("Repository at [%s] does not have a branch [%s]" % (pth, BRANCH_DB))
    except git.exc.InvalidGitRepositoryError:
        repo = git.Repo.init(pth, bare=True)
        commit(repo.index, "Initial empty content")
        repo.create_head(BRANCH_DB)
        for f,pth in utils.DOCUMENTS_PATH.items():
            add_file(repo, pth, b"[]")
        for f,pth in utils.BLOBS_PATH.items():
            add_file(repo, pth, utils.BLOBS_BOOTSTRAP[f])
        commit(repo.index, "Initial empty config")
        repo.create_head(BRANCH_BASE)
    return repo

class ThreadAndProcessLock(object):
    def __init__(self, lockfile):
        self._lockfile = lockfile
        self.tlock = threading.Lock()
        self.flock = fasteners.InterProcessLock('/tmp/tmp_lock_file')
    def __enter__(self):
        self.tlock.__enter__()
        self.flock.__enter__()
    def __exit__(self, *args):
        self.flock.__exit__(*args)
        self.tlock.__exit__(*args)

@Backends.register("git")
class GitBackend(CurieBackend):
    def __init__(self, app, url):
        CurieBackend.__init__(self, app, url)
        self.parsed_url = urllib.parse.urlparse(url)
        if self.parsed_url.netloc != "" or self.parsed_url.scheme != "git":
            raise CurieGitBackendException("Bad URL format [%s]. (eg: git:///path/to/repo)")
        repopath = self.parsed_url.path
        self.repo = get_repo(repopath)
        lockpath = os.path.join(repopath, ".curieconf_lock")
        self.repo.lock = ThreadAndProcessLock(lockpath)


### Helpers


    def _do_prepare_branch(self, branchname):
        branch = self.repo.heads[branchname]
        self.repo.head.reference = branch
        self.repo.index.reset()
        return branch

    def prepare_internal_branch(self, branchname):
        if branchname not in self.repo.heads or not is_internal(branchname):
            abort(404, "internal branch [%s] does not exist" % branchname)
        return self._do_prepare_branch(branchname)

    def prepare_branch(self, branchname):
        if branchname not in self.repo.heads or is_internal(branchname):
            abort(404, "configuration [%s] does not exist" % branchname)
        return self._do_prepare_branch(branchname)


    def get_tree(self, version=None):
        commit = self.repo.head.commit if version is None else self.repo.commit(version)
        return commit.tree

    def add_file(self, fname, content):
        add_file(self.repo, fname, content)

    def add_json_file(self, fname, content, **kargs):
        add_file(self.repo, fname, json.dumps(content, **kargs).encode("utf-8"))

    def del_file(self, fname):
        self.repo.index.remove([fname])

    def get_document(self, doc, version=None):
        if doc not in utils.DOCUMENTS_PATH:
            abort(404, "document [%s] does not exist" % doc)
        rpath = utils.DOCUMENTS_PATH[doc]
        try:
            gitblob = self.get_tree(version)/rpath
        except KeyError:
            rlist = []
        else:
            rlist = json.load(gitblob.data_stream)
        return rlist

    def add_document(self, doc, content):
        if doc not in utils.DOCUMENTS_PATH:
            abort(404, "document type [%s] does not exist" % doc)
        rpath = utils.DOCUMENTS_PATH[doc]
        self.add_json_file(rpath, content, indent=4)

    def del_document(self, doc):
        if doc not in utils.DOCUMENTS_PATH:
            abort(404, "document type [%s] does not exist" % doc)
        rpath = utils.DOCUMENTS_PATH[doc]
        self.del_file(rpath)

    def update_doc(self, doc, data):
        udoc = doc[:]
        enew = { e["id"]:e for e in data }
        for i,e in enumerate(doc):
            if e["id"] in enew:
                udoc[i] = enew.pop(e["id"])
        udoc += list(enew.values())
        return udoc

    def _get_blob(self, blobname, version=None):
        bpath = utils.BLOBS_PATH[blobname]
        gitblob = self.get_tree(version)/bpath
        hint = "json" if bpath.endswith(".json") else None
        return utils.bytes2jblob(gitblob.data_stream.read(), fmthint=hint)

    def get_blob(self, blobname, version=None):
        if blobname not in utils.BLOBS_PATH:
            abort(404, "blob type [%s] does not exist" % blobname)
        try:
            return self._get_blob(blobname, version)
        except KeyError:
            abort(404, "blob [%s] does not exist" % blobname)

    def add_blob(self, blobname, jblob):
        if blobname not in utils.BLOBS_PATH:
            abort(404, "blob type [%s] does not exist" % blobname)
        bpath = utils.BLOBS_PATH[blobname]
        try:
            blob = utils.jblob2bytes(jblob)
        except:
            abort(400, "could not decode blob [%s]" % blobname)

        self.add_file(bpath, blob)

    def del_blob(self, blobname):
        if blobname not in utils.BLOBS_PATH:
            abort(404, "blob type [%s] does not exist" % blobname)
        bpath = utils.BLOBS_PATH[blobname]
        self.del_file(bpath)


    def exists(self, fname, version=None):
        try:
            gb = self.get_tree(version)/fname
        except KeyError:
            return False
        return True

    def doc_exists(self, docname):
        if docname not in utils.DOCUMENTS_PATH:
            return False
        return self.exists(utils.DOCUMENTS_PATH[docname])

    def blob_exists(self, blobname):
        if blobname not in utils.BLOBS_PATH:
            return False
        return self.exists(utils.BLOBS_PATH[blobname])

    def commit(self, msg):
        commit(self.repo.index, msg)

    def get_logs(self, head=None, doc=None, blob=None):
        if head is None:
            head = self.repo.head
        if doc is None and blob is None:
            rpath = None
        else:
            if doc:
                if doc not in utils.DOCUMENTS_PATH:
                    abort(404, "document [%s] does not exist" % doc)
                rpath = utils.DOCUMENTS_PATH[doc]
            else:
                if blob not in utils.BLOBS_PATH:
                    abort(404, "blob [%s] does not exist" % blob)
                rpath = utils.BLOBS_PATH[blob]

        return [ {
            "author": c.author.name,
            "email": c.author.email,
            "date": c.committed_datetime.isoformat(),
            "message": c.message,
            "version": c.hexsha,
            "parents": [pc.hexsha for pc in c.parents]
        } for c in self.repo.iter_commits(rev=head, paths=rpath) ]

    def get_db(self, dbname, version=None):
        try:
            db = self.get_tree(version)/dbname
        except KeyError:
            abort(404, "database [%s] does not exist" % dbname)
        return json.load(db.data_stream)

    def gitpush(self, url):
        remotename = "tmpremote"
        with self.repo.lock:
            # delete remote if it already exists
            remote = None
            for r in git.Remote.iter_items(self.repo):
                if r.name == remotename:
                    remote = r
            if remote is not None:
                git.Remote.remove(self.repo, r)
            # push to remote
            remote = git.Remote.create(self.repo, remotename, url)
            remote.push(all=True)
            remote.push(tags=True)
            git.Remote.remove(self.repo, remotename)

    def gitfetch(self, url):
        remotename = "tmpremote"
        with self.repo.lock:
            # delete remote if it already exists
            remote = None
            for r in git.Remote.iter_items(self.repo):
                if r.name == remotename:
                    remote = r
            if remote is not None:
                git.Remote.remove(self.repo, r)
            # push to remote
            remote = git.Remote.create(self.repo, remotename, url)
            remote.fetch()
            git.Remote.remove(self.repo, remotename)


### CONFIGS

    def configs_list(self):
        with self.repo.lock:
            res = []
            for h in self.repo.heads:
                if is_internal(h.name):
                    continue
                res.append({
                    "id": h.name,
                    "version": h.commit.hexsha,
                    "date": h.commit.committed_datetime.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "description": h.commit.summary,
                    "logs": self.get_logs(h),
                })
        return res

    def configs_list_versions(self, config):
        with self.repo.lock:
            branch = self.prepare_branch(config)
            return self.get_logs()


    def configs_get(self, config, version=None):
        with self.repo.lock:
            branch = self.prepare_branch(config)
            commit = branch.commit if version is None else self.repo.commit(version)
            cfg = {"meta": {
                "id": config,
                "version": commit.hexsha,
                "date": commit.committed_datetime.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "description": commit.summary
            }}
            cfg["documents"] = {
                rname: self.get_document(rname, version)
                for rname in utils.DOCUMENTS_PATH
            }
            blobs = cfg["blobs"] = {}
            for bname in utils.BLOBS_PATH:
                try:
                    blobs[bname] = self._get_blob(bname, version)
                except KeyError:
                    continue
        return cfg


    def configs_create(self, data, name=None):
        if name is None:
            name = data["meta"]["id"]
        with self.repo.lock:
            if name.startswith(INTERNAL_PREFIX):
                abort(400, "config name [%s] cannot start with [%s]" % (name, INTERNAL_PREFIX))
            if name in self.repo.heads:
                abort(409, "config [%s] already exists" % name)
            self.repo.create_head(name, self.repo.commit(BRANCH_BASE))
            self.prepare_branch(name)
            for docname, content in data.get("documents",{}).items():
                self.add_document(docname, content)
            for blobname, jblob in data.get("blobs",{}).items():
                self.add_blob(blobname, jblob)
            self.commit("Create config [%s]" % name)
        return {"ok": True }

    def configs_update(self, config, data):
        with self.repo.lock:
            branch = self.prepare_branch(config)
            new_id = data.get("meta",{}).get("id")
            renamed = ""
            if new_id and new_id != config:
                branch.rename(new_id)
                renamed = " renamed into [%s]" % new_id
            delb = data.get("delete_blobs",{})
            addb = data.get("blobs",{})
            for blobname in utils.BLOBS_PATH:
                if blobname in delb:
                    if delb[blobname] is True:
                        self.del_blob(blobname)
                        continue
                if blobname in addb:
                    self.add_blob(blobname, addb[blobname])

            deld = data.get("delete_documents",{})
            addd = data.get("documents",{})
            for docname in utils.DOCUMENTS_PATH:
                if docname in addd or docname in deld:
                    doc = self.get_document(docname)
                    if docname in addd:
                        doc = self.update_doc(doc, addd[docname])
                    if docname in deld:
                        deleid = { eid for eid,val in deld[docname].items() if val is True }
                        doc = [ entry for entry in doc if entry["id"] not in deleid ]
                    self.add_document(docname, doc)
            self.commit("Update config [%s]%s" % (config, renamed))
        return {"ok": True }

    def configs_delete(self, name):
        with self.repo.lock:
            if name.startswith(INTERNAL_PREFIX):
                abort(400, "config name [%s] cannot start with [%s]" % (name, INTERNAL_PREFIX))
            if name not in self.repo.heads:
                abort(404, "config [%s] does not exists" % name)
            self.repo.delete_head(name, force=True)
        return { "ok": True }

    def configs_clone(self, config, data, new_name=None):
        if new_name is None:
            new_name = data["id"]
        if new_name in self.repo.heads:
            abort(409, "configuration [%s] already exists" % new_name)
        with self.repo.lock:
            self.prepare_branch(config)
            self.repo.create_head(new_name)
        return { "ok": True }

    def configs_revert(self, config, version):
        with self.repo.lock:
            self.prepare_branch(config)
            c = self.repo.commit(version)
            idx = git.index.base.IndexFile.from_tree(self.repo, c.tree)
            commit(idx, "Revert to version [%s]" % version)
        return { "ok": True }

### BLOBS

    def blobs_list(self, config, version=None):
        if version != None:
            raise NotImplementedError
        with self.repo.lock:
            self.prepare_branch(config)
            return [ { "name": blob } for blob in utils.BLOBS_PATH if self.blob_exists(blob) ]

    def blobs_list_versions(self, config, blob):
        with self.repo.lock:
            self.prepare_branch(config)
            return self.get_logs(config, blob=blob)

    def blobs_get(self, config, blob, version=None):
        with self.repo.lock:
            self.prepare_branch(config)
            return self.get_blob(blob, version)


    def blobs_create(self, config, blob, data):
        with self.repo.lock:
            self.prepare_branch(config)
            if self.blob_exists(blob):
                abort(409, "blob [%s] already exists" % blob)
            self.add_blob(blob, data)
            self.commit("Create blob [%s]" % blob)
        return {"ok": True }

    def blobs_update(self, config, blob, data):
        with self.repo.lock:
            self.prepare_branch(config)
            if not self.blob_exists(blob):
                abort(404, "blob [%s] does not exist" % blob)
            self.add_blob(blob, data)
            self.commit("Update blob [%s]" % blob)
        return {"ok": True }

    def blobs_delete(self, config, blob):
        with self.repo.lock:
            self.prepare_branch(config)
            if not self.blob_exists(blob):
                abort(404, "blob [%s] does not exist" % blob)
            self.del_blob(blob)
            self.commit("Delete blob [%s]" % blob)
        return {"ok": True }

    def blobs_revert(self, config, blob, version):
        with self.repo.lock:
            self.prepare_branch(config)
            b = self.get_blob(blob, version)
            self.add_blob(blob, b)
            self.commit("Revert blob [%s] to version [%s]" % (blob, version))
        return { "ok": True }

### DOCUMENTS

    def _documents_check_current_consistency(self, config, added: Dict[str, str] = {}, removed: List[str] = []) -> List[str]:
        docnames = [d["name"] for d in self._documents_list(config) if d["entries"] > 0]
        docs = {}
        for dn in docnames:
            docs[dn] = self.get_document(dn, None)
        # obtain current documents
        for dname, contents in added.items():
            docs[dname] = contents
        for dname in removed:
            if dname in docs:
                del docs[dname]
        # check consistency
        res = []
        referenced_acl_policies = []
        referenced_waf_policies = []
        waf_policies = []
        acl_policies = []
        if "urlmaps" in docs:
            for umap in docs["urlmaps"]:
                for umapmap in umap["map"]:
                    referenced_waf_policies.append(umapmap["waf_profile"])
                    referenced_acl_policies.append(umapmap["acl_profile"])
        if "wafpolicies" in docs:
            for waf_policy in docs["wafpolicies"]:
                waf_policies.append(waf_policy["id"])
        if "aclpolicies" in docs:
            for acl_policy in docs["aclpolicies"]:
                acl_policies.append(acl_policy["id"])
        for acl_policy in referenced_acl_policies:
            if acl_policy not in acl_policies:
                res.append(f"ACL Profile {acl_policy} is referenced but not present")
        for waf_policy in referenced_waf_policies:
            if waf_policy not in waf_policies:
                res.append(f"WAF Profile {waf_policy} is referenced but not present")
        return res

    def documents_list(self, config, version=None):
        if version is not None:
            raise NotImplementedError
        with self.repo.lock:
            return self._documents_list(config, version)

    def _documents_list(self, config, version=None):
        # Assumes lock is held
        self.prepare_branch(config)
        res = []
        for doc in utils.DOCUMENTS_PATH:
            if self.doc_exists(doc):
                docdata = self.get_document(doc, version)
                res.append({"name": doc, "entries": len(docdata)})
        return res

    def documents_list_versions(self, config, document):
        with self.repo.lock:
            self.prepare_branch(config)
            return self.get_logs(config, doc=document)

    def documents_get(self, config, document, version=None):
        with self.repo.lock:
            self.prepare_branch(config)
            return self.get_document(document, version)

    def documents_create(self, config, document, data):
        with self.repo.lock:
            self.prepare_branch(config)
            if self.doc_exists(document):
                if self.get_document(document) != []:
                    abort(409, "document [%s] already exists" % document)
            errors = self._documents_check_current_consistency(
                config, added={document: data})
            if errors:
                return {"ok": False, "errors": errors}
            self.add_document(document, data)
            self.commit("New version of document [%s]" % document)
        return {"ok": True}

    def documents_update(self, config, document, data):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            updated_doc = self.update_doc(doc, data)
            errors = self._documents_check_current_consistency(
                config, added={document: data})
            if errors:
                return {"ok": False, "errors": errors}
            self.add_document(document, updated_doc)
            self.commit("Update document [%s]" % document)
        return {"ok": True}

    def documents_delete(self, config, document):
        with self.repo.lock:
            self.prepare_branch(config)
            if not self.doc_exists(document):
                abort(404, "document [%s] does not exist" % document)
            errors = self._documents_check_current_consistency(
                config, removed=[document])
            if errors:
                return {"ok": False, "errors": errors}
            self.add_document(document, [])
            self.commit("Delete document [%s]" % document)
        return {"ok": True}

    def documents_revert(self, config, document, version):
        with self.repo.lock:
            self.prepare_branch(config)
            d = self.get_document(document, version)
            errors = self._documents_check_current_consistency(
                config, added={document: d})
            if errors:
                return {"ok": False, "errors": errors}
            self.add_document(document, d)
            self.commit("Revert document [%s] to version [%s]" % (document, version))
        return {"ok": True}

### ENTRIES

    def entries_list(self, config, document, version=None):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            return [ e["id"] for e in doc ]

    def entries_list_versions(self, config, document, entry):
        with self.repo.lock:
            self.prepare_branch(config)
            allvers = self.get_logs(config, doc=document)
            ent = {}
            res = []
            vprec = allvers.pop()
            dprec = [ e for e in self.get_document(document, vprec["version"]) if e["id"] == entry ]
            if dprec:
                res.append(vprec)
            while allvers:
                v = allvers.pop()
                d = [ e for e in self.get_document(document, v["version"]) if e["id"] == entry ]
                if len(d) != len(dprec):
                    res.append(v)
                else:
                    if d:
                        if d != dprec:
                            res.append(v)
                vprec = v
                dprec = d
            res.reverse()
            return res

    def entries_get(self, config, document, entry, version=None):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            for e in doc:
                if e["id"] == entry:
                    return e
            else:
                abort(404, "Entry [%s] does not exist" % entry)

    def entries_create(self, config, document, data):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            for e in doc:
                if e["id"] == data["id"]:
                    abort(409, "entry [%s] already exists" % data["id"])
            else:
                doc.append(data)
            self.add_document(document, doc)
            self.commit("Add entry [%s] to document [%s]" % (data["id"], document))
        return { "ok": True }

    def entries_update(self, config, document, entry, data):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            for i,e in enumerate(doc):
                if e["id"] == entry:
                    break
            else:
                abort(404, "entry [%s] does not exist" % entry)

            doc[i] = data

            self.add_document(document, doc)
            if data["id"] == entry:
                msg = "Update entry [%s] of document [%s]" % (entry, document)
            else:
                msg = "Update entry [%s] into entry [%s] in document [%s]" % (entry, data["id"], document)
            self.commit(msg)
        return { "ok": True }


    def entries_delete(self, config, document, entry):
        with self.repo.lock:
            self.prepare_branch(config)
            doc = self.get_document(document)
            for i,e in enumerate(doc):
                if e["id"] == entry:
                    break
            else:
                abort(404, "entry [%s] does not exist" % entry)
            del(doc[i])
            self.add_document(document, doc)
            self.commit("Delete entry [%s] of document [%s]" % (entry, document))
        return { "ok": True }


### DATABASES

    def db_list(self, version=None):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            t = self.get_tree(version)
            return [ obj.name for obj in t.traverse() if obj.type == "blob" ]

    def db_list_versions(self):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            return self.get_logs()

    def db_get(self, dbname, version=None):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            return self.get_db(dbname, version)

    def db_create(self, dbname, data):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            if self.exists(dbname):
                abort(409, "database [%s] already exists" % dbname)
            self.add_json_file(dbname, data)
            self.commit("Added database [%s]" % dbname)
        return { "ok": True }


    def db_update(self, dbname, data):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            try:
                dbobj = self.get_tree()/dbname
            except KeyError:
                db = {}
            else:
                db = json.load(dbobj.data_stream)
            db.update(data)
            self.add_json_file(dbname, db)
            self.commit("Updated database [%s]" % dbname)
        return { "ok": True }

    def db_delete(self, dbname):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            if self.exists(dbname):
                self.del_file(dbname)
                self.commit("Deleted database [%s]" % dbname)
            else:
                abort(409, "database [%s] does not exist" % dbname)
        return { "ok": True }

    def db_revert(self, dbname, version):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            try:
                dbobj = self.get_tree(version)/dbname
            except KeyError:
                abort(404, "database [%s] version [%s] not found"  %  (dbname, version))
            self.add_file(dbname, dbobj.data_stream.read())
            self.commit("Reverting database [%s] to version [%s]" % (dbname, version))
        return { "ok": True }

    def db_query(self, dbname, query):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            db = self.get_db(dbname)
            return jmespath.search(query, db)

### KEYS

    def key_list(self, dbname):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            db = self.get_db(dbname)
            return list(db.keys())

    def key_list_versions(self, dbname, key):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            allvers = self.get_logs()
            res = []
            vprec = {}
            dprec = []
            try:
                vprec = allvers.pop()
                db = self.get_db(dbname, version=vprec["version"])
                dprec = [ db[k] for k in db if k == key ]
                if dprec:
                    res.append(vprec)
            except:
                pass
            while allvers:
                try:
                    v = allvers.pop()
                    db = self.get_db(dbname, version=v["version"])
                    d = [ db[k] for k in db if k == key ]
                    if len(d) != len(dprec):
                        res.append(v)
                    else:
                        if d:
                            if d != dprec:
                                res.append(v)
                    vprec = v
                    dprec = d
                except:
                    pass
            res.reverse()
            return res

    def key_get(self, dbname, key, version=None):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            db = self.get_db(dbname, version)
            try:
                return db[key]
            except KeyError:
                abort(404, "Key [%s] not found in database [%s]" % (key, dbname))

    def key_set(self, dbname, key, value):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            db = self.get_db(dbname)
            db[key] = value
            self.add_json_file(dbname, db)
            self.commit("Setting key [%s] in database [%s]" % (key, dbname))
        return { "ok": True }

    def key_delete(self, dbname, key):
        with self.repo.lock:
            self.prepare_internal_branch(BRANCH_DB)
            db = self.get_db(dbname)
            try:
                del(db[key])
            except KeyError:
                abort(404, "Key [%s] not found in database [%s]" % (key, dbname))
            self.add_json_file(dbname, db)
            self.commit("Deleting key [%s] in  database [%s]" % (key, dbname))
        return { "ok": True }
