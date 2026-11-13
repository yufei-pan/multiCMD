#!/usr/bin/env python3
import _thread,argparse,atexit,collections,collections.abc,errno,io,itertools,math,os,re,select,shlex,shutil,signal,stat,string,subprocess,sys,threading,time,traceback
from pprint import pformat
version='1.64'
__version__=version
COMMIT_DATE='2026-11-13'
__running_threads=set()
__finished_threads=collections.deque()
_JOIN_MAY_HANG=sys.version_info<(3,9,8)or sys.version_info[:3]==(3,10,0)
_BRACKET_RX=re.compile('\\[([^\\]]+)\\]')
_ALPHANUM=string.digits+string.ascii_letters
_ALPHA_IDX={c:i for(i,c)in enumerate(_ALPHANUM)}
_UNESCAPED_COLON_RX=re.compile('(?<!\\\\):')
SUDO_PATH=shutil.which('sudo')
USE_SUDO=False
_IS_POSIX=os.name=='posix'
_HAS_PROCESS_GROUP=sys.version_info>=(3,11)
_ENV_VAR='_MULTICMD_ENV_'
_PGROUP_EXEC="import posix,_signal,sys\nfor n in ('SIGPIPE','SIGXFSZ'):\n\tif hasattr(_signal,n):_signal.signal(getattr(_signal,n),_signal.SIG_DFL)\nposix.setpgid(0,0)\na=sys.argv\nh=int(a[2])\nb=posix.read(h,1)\nif not b:posix._exit(125)\nposix.close(h)\nif b==b'2':\n\tif _signal.getsignal(_signal.SIGINT)!=_signal.SIG_IGN:_signal.signal(_signal.SIGINT,_signal.SIG_DFL)\n\t_signal.pthread_sigmask(_signal.SIG_UNBLOCK,(_signal.SIGINT,))\nc=a[3]\nv=None\nif a[1]!='-':\n\ttry:\n\t\tv=dict(posix.environ[b'_MULTICMD_ENV_%d'%i].split(b'=',1) for i in range(int(a[1])))\n\texcept(KeyError,ValueError):\n\t\tsys.stderr.write('multiCMD: incomplete environment for %r\\n'%c);sys.exit(126)\ndef x(f):\n\tif v is None:posix.execv(f,a[3:])\n\tposix.execve(f,a[3:],v)\ndef g(f):\n\ttry:return posix.stat(f).st_mode&0o170000==0o100000\n\texcept(OSError,ValueError):return False\ntry:\n\tif '/' in c:x(c)\n\tp=(posix.environ if v is None else v).get(b'PATH')\n\tr=None\n\tfor d in ("+repr(os.defpath)+" if p is None else p.decode(sys.getfilesystemencoding(),'surrogateescape')).split(':'):\n\t\td=d or '.'\n\t\tf=d+c if d.endswith('/') else d+'/'+c\n\t\tif g(f):\n\t\t\ttry:x(f)\n\t\t\texcept(FileNotFoundError,NotADirectoryError):pass\n\t\t\texcept OSError as e:r=r or e\n\traise r or FileNotFoundError(2,posix.strerror(2),c)\nexcept OSError as e:\n\tsys.stderr.write((str(e) if a[1]=='-' else str(OSError(e.errno,e.strerror,c)))+'\\n')\n\tsys.exit(127 if isinstance(e,FileNotFoundError) else 126)\n"
_TTY_SIGNALS={signal.SIGTTIN,signal.SIGTTOU}if _IS_POSIX else set()
try:import _signal
except ImportError:_signal=None
_sigmask=getattr(_signal,'pthread_sigmask',None)or getattr(signal,'pthread_sigmask',None)
_live_procs={}
_draining={}
_PTY_WARNED=False
_exiting=False
class Task:
	def __init__(self,command):self.command=command;self.returncode=None;self.stdout=[];self.stderr=[];self.thread=None;self.stop=False;self.start_time=None;self.end_time=None;self._last_output=None;self._finalized=False;self._pty=False;self._error=False;self._env=None;self._extra_env=None;self._pass_fds=();self._at_exit=False;self._unread=[]
	def __iter__(self):yield from(('command',self.command),('returncode',self.returncode),('stdout',self.stdout),('stderr',self.stderr))
	def __repr__(self):return f"""Task(command={pformat(self.command)},
returncode={self.returncode!r},
stdout=
{pformat(self.stdout)},
stderr=
{pformat(self.stderr)},
stop={self.stop!r})"""
	def __str__(self):return pformat(dict(self))
	def is_alive(self):return self.thread is not None and self.thread.is_alive()
	@property
	def duration(self):
		if self.start_time is None or self.end_time is None:return
		return self.end_time-self.start_time
class CommandError(Exception):
	def __init__(self,tasks,total=None):
		self.tasks=list(tasks);total=len(self.tasks)if total is None else total;lines=[f"Commands failed ({len(self.tasks)} of {total}):"]
		for task in self.tasks:
			line=f"  [rc {task.returncode}] {_quote_command(task.command)}";last=_last_output_line(task)
			if last:line+=': '+last
			lines.append(line)
		super().__init__('\n'.join(lines))
class _HeldPermit:
	def __init__(self,semaphore):self.semaphore=semaphore
	def __enter__(self):return self
	def __exit__(self,*exc_info):self.semaphore.release();return False
class _Done:
	def __init__(self):self.flag=False;self._wake=threading.Lock();self._wake.acquire();self._released={}
	def is_set(self):return self.flag
	def set(self):
		self.flag=True;mine=object()
		if self._released.setdefault('by',mine)is mine:self._wake.release()
	def wait(self,timeout=None):
		deadline=None if timeout is None else time.monotonic()+timeout
		while not self.flag:
			step=.1 if deadline is None else min(.1,deadline-time.monotonic())
			if step<=0:return False
			if self._wake.acquire(True,step):self._wake.release()
		return True
_START_WAIT=1
def _start_in_helper(thread,done,failed):
	try:threading.Thread.start(thread)
	except BaseException as e:failed.append(e)
	finally:done.set()
class _Thread(threading.Thread):
	def __init__(self,target,args=(),task=None):super().__init__(target=target,args=args,daemon=True);self.task=task;self.done=_Done();self._pid=os.getpid()
	def start(self):
		if _thread.get_ident()!=threading.main_thread().ident:return super().start()
		done,failed,helper=_Done(),[],[]
		try:helper.extend(map(_thread.start_new_thread,(_start_in_helper,),((self,done,failed),)));done.wait()
		except BaseException:
			if helper:done.wait(_START_WAIT)
			raise
		if failed:raise failed[0]
	def run(self):
		try:super().run()
		finally:self.done.set()
	def is_alive(self):return self._started.is_set()and not self.done.flag and self._pid==os.getpid()
	def join(self,timeout=None):
		if self._pid!=os.getpid()or not self._started.is_set():return
		deadline=None if timeout is None else time.monotonic()+timeout
		if not self.done.wait(timeout):return
		left=None if deadline is None else max(0,deadline-time.monotonic())
		if _JOIN_MAY_HANG:left=1 if left is None else min(left,1)
		super().join(left)
class _Reader:
	__slots__='_target','_args','done','_started','_pid'
	def __init__(self,target,args=()):self._target,self._args=target,args;self.done=_Done();self._started=_Done();self._pid=os.getpid()
	def start(self):_thread.start_new_thread(self._run,());self._started.set()
	def _run(self):
		self._started.set()
		try:self._target(*self._args)
		finally:self.done.set()
	def is_alive(self):return self._started.flag and not self.done.flag and self._pid==os.getpid()
	def join(self,timeout=None):
		if self._pid==os.getpid()and self._started.flag:self.done.wait(timeout)
def _any_alive(threads):
	for thread in threads:
		if thread.is_alive():return True
	return False
def _check_started(threads):
	for thread in threads:
		if not isinstance(thread,_Thread)and thread.ident is None:raise RuntimeError('cannot join thread before it is started')
class _Display:
	def __init__(self):self.lock=threading.Lock();self.redrawable=False;self.at_line_start=True;self.cli=False;self.closed=False;self.lost=False;self.watch=None
	def _write(self,text):
		stdout=sys.stdout
		if self.closed or self.lost or stdout is None:return
		try:
			try:stdout.write(text)
			except UnicodeEncodeError:encoding=getattr(stdout,'encoding',None)or'ascii';stdout.write(text.encode(encoding,'backslashreplace').decode(encoding))
			stdout.flush()
		except(OSError,ValueError)as e:
			if isinstance(e,OSError)and _no_reader(e):self.closed=True
			else:self.lose(e)
	def lose(self,error):
		if self.lost:return
		self.lost=True;_write_error(f"{'mcmd'if self.cli else'multiCMD'}: cannot write to stdout ({error}): its output is dropped from here on")
	def flush(self):self._write('')
	def lines(self,texts,decor=('','',''),replace_first=False,ends_with_cr=False):
		prefix,start,end=decor
		with self.lock:
			parts=[]
			for(i,text)in enumerate(texts):
				if i==0 and replace_first and self.redrawable:parts.append('\r'+prefix+start+text+end+('\x1b[K'if _stdout_isatty()else''))
				else:parts.append('\n'+prefix+start+text+end)
			self.redrawable=ends_with_cr;self.at_line_start=False;self._write(''.join(parts))
	def message(self,text,decor=('','','')):
		prefix,start,end=decor
		with self.lock:lead=''if self.at_line_start else'\n';body='\n'.join(prefix+start+line+end for line in text.split('\n'));self._write(lead+body+'\n');self.redrawable=False;self.at_line_start=True
	def settle(self):
		with self.lock:self.redrawable=False
_display=_Display()
def _no_reader(error):
	if isinstance(error,(BrokenPipeError,ConnectionResetError)):return True
	return not _IS_POSIX and error.errno==errno.EINVAL and _stdout_is_pipe()
def _stdout_is_pipe():
	try:return stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
	except(AttributeError,OSError,ValueError):return False
_READER_QUIT=getattr(select,'POLLERR',0)|getattr(select,'POLLHUP',0)
def _watch_stdout():
	if not hasattr(select,'poll')or not _stdout_is_pipe():return
	watch=select.poll();watch.register(sys.stdout.fileno(),_READER_QUIT);return watch
def _reader_quit():
	if not _display.cli:return False
	if not _display.closed and _display.watch is not None and _thread.get_ident()==threading.main_thread().ident:
		for(_,events)in _display.watch.poll(0):
			if events&_READER_QUIT:_display.closed=True
	return _display.closed
def _stops(task):return task.stop or task._unread or _reader_quit()
class AsyncExecutor:
	def __init__(self,max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False,shell=False,prefix=False,color=None,pty=False,env=None,extra_env=None,pass_fds=()):
		self.max_threads=max_threads
		if semaphore is...:semaphore=threading.Semaphore(max_threads)
		self.semaphore=semaphore;self.tasks=[];self.timeout=timeout;self.quiet=quiet;self.dry_run=dry_run;self.parse=parse;self.shell=shell;self.prefix=prefix;self.color=color;self.pty=pty;env,extra=_check_env(env,'env',drop_empty=True),_check_env(extra_env,'extra_env',allow_none=True);self.env=None if env is None else env.copy();self.extra_env=None if extra_env is None else dict(extra or{});self.pass_fds=_check_pass_fds(pass_fds);self._lock=threading.Lock();self._queue=collections.deque();self._dispatching=False;self._threads={};self._finished=collections.deque()
	def __iter__(self):return iter(self.tasks)
	def __repr__(self):return f"AsyncExecutor(max_threads={self.max_threads}, semaphore={self.semaphore}, runningThreads={self.runningThreads}, tasks={self.tasks}, timeout={self.timeout}, quiet={self.quiet}, dry_run={self.dry_run}, parse={self.parse})"
	def __str__(self):return str(self.tasks)
	def __len__(self):return len(self.tasks)
	def __bool__(self):return bool(self.tasks)
	@property
	def runningThreads(self):self._reap();return list(self._threads.copy())
	def _reap(self):
		while True:
			try:thread,task=self._finished.popleft()
			except IndexError:return
			try:thread.join()
			finally:
				self._threads.pop(thread,None)
				if task is not None and task.thread is thread:task.thread=None
	def _track(self,thread):self._threads[thread]=None
	def _untrack(self,thread):self._threads.pop(thread,None)
	def _dispatch(self):
		try:
			while True:
				with self._lock:
					if not self._queue:self._dispatching=False;break
					task,sem,timeout,quiet,dry_run,decor,use_pty=self._queue.popleft()
				acquired=False
				while not task.stop and not acquired:acquired=sem.acquire(timeout=.1)
				if task.stop:
					if acquired:sem.release()
					task._env=task._extra_env=None;continue
				thread=None
				try:self._reap();thread=_Thread(_run_executor_task,(task,_HeldPermit(sem),timeout,quiet,dry_run,self._finished,decor,use_pty),task);self._track(thread);thread.start()
				except Exception as e:
					if thread is not None:self._untrack(thread)
					sem.release();_fail_task(task,f"cannot start its thread: {e}",e,quiet)
		except BaseException:
			with self._lock:self._dispatching=False
			raise
		finally:self._finished.append((threading.current_thread(),None))
	def run_commands(self,commands,timeout=...,max_threads=...,quiet=...,dry_run=...,parse=...,sem=...,shell=...,prefix=...,color=...,pty=...,env=...,extra_env=...,pass_fds=...):
		if timeout is...:timeout=self.timeout
		if max_threads is...:max_threads=self.max_threads
		if quiet is...:quiet=self.quiet
		if dry_run is...:dry_run=self.dry_run
		if parse is...:parse=self.parse
		if sem is...:sem=self.semaphore
		if shell is...:shell=self.shell
		if prefix is...:prefix=self.prefix
		if color is...:color=self.color
		if pty is...:pty=self.pty
		if env is...:env=self.env
		if extra_env is...:extra_env=self.extra_env
		if pass_fds is...:pass_fds=self.pass_fds
		taskObjects,use_sudo=_build_tasks(commands,parse=parse,shell=shell,env=env,extra_env=extra_env,pass_fds=pass_fds)
		if not sem:sem=threading.Semaphore(max_threads if max_threads>=1 else max(len(taskObjects),1))
		if use_sudo and not dry_run:_validate_sudo()
		concurrent=max_threads!=1;use_color=_resolve_color(color,concurrent);use_pty=_resolve_pty(pty);self._reap();start_dispatcher=False;dispatcher=None
		try:
			with self._lock:
				first=len(self.tasks)+1;queued=[(task,sem,timeout,quiet,dry_run,_decor(first+i,0,prefix and concurrent,use_color),use_pty)for(i,task)in enumerate(taskObjects)];self.tasks.extend(taskObjects);self._queue.extend(queued)
				if not self._dispatching and self._queue:start_dispatcher=True;self._dispatching=True
			if start_dispatcher:dispatcher=_Thread(self._dispatch);self._track(dispatcher);dispatcher.start()
		except BaseException:
			_stop_tasks(taskObjects)
			if start_dispatcher and(dispatcher is None or not dispatcher._started.is_set()):
				if dispatcher is not None:self._untrack(dispatcher)
				self._dispatching=False
			raise
		return taskObjects
	def run_command(self,command,timeout=...,max_threads=...,quiet=...,dry_run=...,parse=...,sem=...,shell=...,prefix=...,color=...,pty=...,env=...,extra_env=...,pass_fds=...):return self.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem,shell=shell,prefix=prefix,color=color,pty=pty,env=env,extra_env=extra_env,pass_fds=pass_fds)[0]
	def wait(self,timeout=None,threads=...):
		deadline=None if timeout is None or timeout<0 else time.monotonic()+timeout
		if threads is not...:threads=list(threads);_check_started(threads)
		try:self._wait_until(deadline,threads)
		except BaseException:_stop_tasks(self.tasks);self._wait_until(time.monotonic()+5);raise
		return[thread for thread in self.runningThreads if thread.is_alive()]
	def _wait_until(self,deadline,threads=...):
		joined=set()
		while True:
			pending=self.runningThreads if threads is...else list(threads)
			for thread in pending:_join(thread,None if deadline is None else max(0,deadline-time.monotonic()))
			if threads is not...or not pending or _any_alive(pending)or joined.issuperset(pending)or deadline is not None and time.monotonic()>=deadline:return
			joined.update(pending)
	def stop(self,timeout=None):_stop_tasks(self.tasks);self.wait(timeout);return self.tasks
	def cleanup(self,timeout=None):
		self.stop(timeout)
		with self._lock:
			for entry in self._queue:entry[0]._env=entry[0]._extra_env=None
			self.tasks=[];self._queue.clear();self._threads.clear()
		return self.tasks
	def join(self,timeout=None,threads=...,print_error=True):
		self.wait(timeout=timeout,threads=threads)
		if print_error:
			for task in self.tasks:
				if not task._finalized:
					if task.stop and task.thread is None:_display.message(f"Command: {task.command} skipped: stopped before it started")
					else:_display.message(f"Command: {task.command} still running")
				elif task.returncode not in(0,None):_display.message(f"Command: {task.command} failed with return code: {task.returncode}\nStdout:\n"+'\n  '.join(task.stdout)+'\nStderr:\n'+'\n  '.join(task.stderr))
		return self.tasks
	def get_results(self,with_stdErr=False):
		if with_stdErr:return[task.stdout+task.stderr for task in self.tasks]
		else:return[task.stdout for task in self.tasks]
	def get_return_codes(self):return[task.returncode for task in self.tasks]
def __evaluate_value(expr,vars_,dereference=True):
	if expr.startswith('{')and expr.endswith('}'):
		try:return eval(expr[1:-1],{},vars_)
		except Exception:return expr
	elif dereference:return vars_.get(expr,expr)
	else:return expr
def _expand_piece(piece,vars_):
	piece=piece.strip();parts=_UNESCAPED_COLON_RX.split(piece)
	if len(parts)==2 and parts[0].isidentifier():vars_[parts[0]]=__evaluate_value(parts[1].strip().replace('\\:',':'),vars_,dereference=False);return
	piece=piece.replace('\\:',':')
	if'-'in piece:
		start,_,end=(p.strip()for p in piece.partition('-'));start=str(__evaluate_value(start,vars_));end=str(__evaluate_value(end,vars_))
		if start.isdigit()and end.isdigit():pad=min(len(start),len(end));return[f"{i:0{pad}d}"for i in range(int(start),int(end)+1)]
		if start in _ALPHA_IDX and end in _ALPHA_IDX:return[_ALPHANUM[i]for i in range(_ALPHA_IDX[start],_ALPHA_IDX[end]+1)]
		if start and end and all(c in string.hexdigits for c in start+end):upper=''.join(c for c in start+end if c.isalpha()).isupper();pad=min(len(start),len(end));return[format(i,f"0{pad}{'X'if upper else'x'}")for i in range(int(start,16),int(end,16)+1)]
	value=__evaluate_value(piece,vars_)
	if isinstance(value,str):return[value]
	else:
		try:return[str(v)for v in value]
		except Exception:return[str(value)]
def _expand_ranges_fast(inStr,variables=None):
	if variables is None:variables={}
	segments=[];pos=0
	for match in _BRACKET_RX.finditer(inStr):
		if match.start()>pos:segments.append([inStr[pos:match.start()]])
		choices=[]
		for sub in match.group(1).split(','):
			expandedPieces=_expand_piece(sub,variables)
			if expandedPieces:choices.extend(expandedPieces)
		if choices:segments.append(choices)
		pos=match.end()
	segments.append([inStr[pos:]]);return[''.join(parts)for parts in itertools.product(*segments)]
_expand_ranges=_expand_ranges_fast
_LAST_WORDS=.2
def _read_stream(stream,target,task,decor=('','',''),quiet=False):
	def emit(lines,replace_first,ends_with_cr):
		if task._finalized:return
		texts=[line.decode('utf-8',errors='backslashreplace')for line in lines]
		if replace_first:target.pop()
		target.extend(texts)
		if not quiet:_display.lines(texts,decor,replace_first,ends_with_cr)
	def settle():
		if not quiet and not task._finalized:_display.settle()
	lost_at=None;current=bytearray();committed=True
	try:
		while True:
			try:chunk=stream.read1(65536)
			except OSError as e:
				if e.errno==errno.EIO:break
				raise
			if not chunk:break
			task._last_output=time.monotonic()
			if not quiet and _display.closed and not task._finalized:
				if lost_at is None:lost_at=task._last_output
				elif task._last_output-lost_at>_LAST_WORDS and not task._unread:task._unread.append(True)
			if b'\r'not in chunk:
				parts=chunk.split(b'\n')
				if len(parts)==1:current+=chunk;continue
				parts[0]=bytes(current)+parts[0];current=bytearray(parts.pop())
				if committed:emit(parts,False,False)
				elif parts[0]:emit(parts,True,False)
				else:
					settle()
					if len(parts)>1:emit(parts[1:],False,False)
				committed=True;continue
			pos=0;size=len(chunk)
			while pos<size:
				newline=chunk.find(b'\n',pos);carriage=chunk.find(b'\r',pos);end=newline if carriage<0 or 0<=newline<carriage else carriage
				if end<0:current+=chunk[pos:];break
				current+=chunk[pos:end]
				if chunk[end]==10:
					if not committed and current:emit([bytes(current)],True,False)
					elif committed:emit([bytes(current)],False,False)
					else:settle()
					committed=True
				else:emit([bytes(current)],not committed,True);committed=False
				current=bytearray();pos=end+1
		if current:emit([bytes(current)],not committed,False)
	finally:
		try:stream.close()
		except Exception:pass
def int_to_color(hash_value,min_brightness=100,max_brightness=220):
	def _brightness(r,g,b):return math.sqrt(.299*r**2+.587*g**2+.114*b**2)
	r=hash_value>>16&255;g=hash_value>>8&255;b=hash_value&255;brightness=_brightness(r,g,b)
	if brightness==0:gray=int((min_brightness+max_brightness)/2);return gray,gray,gray
	if brightness>max_brightness:scale=max_brightness/brightness;r,g,b=r*scale,g*scale,b*scale
	elif brightness<min_brightness:
		scale=min_brightness/brightness;r,g,b=min(255,r*scale),min(255,g*scale),min(255,b*scale)
		if _brightness(r,g,b)<min_brightness:
			lo,hi=.0,1.
			for _ in range(20):
				t=(lo+hi)/2
				if _brightness(r+t*(255-r),g+t*(255-g),b+t*(255-b))<min_brightness:lo=t
				else:hi=t
			r,g,b=r+hi*(255-r),g+hi*(255-g),b+hi*(255-b)
	return min(255,round(r)),min(255,round(g)),min(255,round(b))
def set_sudo(use_sudo):
	global USE_SUDO;global SUDO_PATH
	if not use_sudo:USE_SUDO=False;return
	if getattr(os,'geteuid',lambda:1)()==0:USE_SUDO=False
	elif SUDO_PATH:USE_SUDO=True
	else:_write_error('sudo not found in PATH, cannot use sudo. ignoring it...');USE_SUDO=False
class _ExecRefused(OSError):0
def _uses_wrapper():return _IS_POSIX and not _HAS_PROCESS_GROUP and bool(sys.executable)and not getattr(sys,'frozen',False)
def _search_dirs(env):path=(os.environ if env is None else env).get('PATH');return(os.defpath if path is None else path).split(os.pathsep)
def _file_named(command,dirs,start=0):
	for index in range(start,len(dirs)):
		path=os.path.join(dirs[index]or os.curdir,command)
		if os.path.isfile(path):return index,path
	return None,None
def _spawn(argv,stdout,stderr,env=None,explicit=False,started=None,pass_fds=()):
	command=argv[0];wrapped=_uses_wrapper();bare=_IS_POSIX and os.sep not in command
	if wrapped and bare and _file_named(command,_search_dirs(env))[1]is None:raise FileNotFoundError(errno.ENOENT,os.strerror(errno.ENOENT),command)
	if wrapped or not bare:
		try:return _spawn_process(argv,stdout,stderr,env,explicit,started=started,pass_fds=pass_fds)
		except OSError as e:
			if wrapped:
				if e.errno==errno.E2BIG:raise _ExecRefused(e.errno,e.strerror,command)from None
				if isinstance(e,(FileNotFoundError,PermissionError)):raise RuntimeError('cannot start the exec wrapper %r: %s'%(sys.executable,e))from e
				raise
			if e.filename is None or isinstance(e,(FileNotFoundError,PermissionError)):raise
			raise _ExecRefused(e.errno,e.strerror,e.filename)from None
	failure=None;dirs=_search_dirs(env);index=0
	while True:
		index,path=_file_named(command,dirs,index)
		if path is None:break
		index+=1
		try:return _spawn_process(argv,stdout,stderr,env,explicit,executable=path,started=started,pass_fds=pass_fds)
		except OSError as e:
			if e.filename is None:raise
			if e.errno==errno.E2BIG:raise _ExecRefused(e.errno,e.strerror,command)from None
			if failure is None and not isinstance(e,(FileNotFoundError,NotADirectoryError)):failure=e
	if failure is None:raise FileNotFoundError(errno.ENOENT,os.strerror(errno.ENOENT),command)
	if isinstance(failure,PermissionError):raise PermissionError(failure.errno,failure.strerror,command)
	raise _ExecRefused(failure.errno,failure.strerror,command)
def _handover_pipe():
	r,w=os.pipe();low=[]
	while r<=2:low.append(r);r=os.dup(r)
	for fd in low:os.close(fd)
	return r,w
def _popen(argv,started=None,grouped=False,handover=None,go=b'1',**kwargs):
	pipe=None
	try:
		if handover is not None:pipe=_handover_pipe();argv[handover]=str(pipe[0]);kwargs['pass_fds']=tuple(kwargs.get('pass_fds',()))+(pipe[0],)
		proc=subprocess.Popen(argv,**kwargs);command=proc,proc.pid if grouped else None
		if started is not None:started.append(command)
		if pipe is not None:os.write(pipe[1],go)
	finally:
		if pipe is not None:os.close(pipe[0]);os.close(pipe[1])
	return command
def _spawn_process(argv,stdout,stderr,env=None,explicit=False,executable=None,started=None,pass_fds=()):
	kwargs={'stdin':subprocess.DEVNULL,'stdout':stdout,'stderr':stderr,'env':env}
	if pass_fds:kwargs['pass_fds']=pass_fds
	if executable is not None:kwargs['executable']=executable
	if not _IS_POSIX:return _popen(argv,started,**kwargs)
	grouped=True;handover=None
	if _HAS_PROCESS_GROUP:kwargs['process_group']=0
	elif _uses_wrapper():
		count='-'
		if explicit:entries=[os.fsencode(k)+b'='+os.fsencode(v)for(k,v)in env.items()];kwargs['env']=dict(_snapshot(os.environb,drop_empty=True));kwargs['env'].update((os.fsencode(_ENV_VAR+str(i)),entry)for(i,entry)in enumerate(entries));count=str(len(entries))
		argv=[sys.executable,'-I','-S','-c',_PGROUP_EXEC,count,None]+list(argv);handover=6
	else:grouped=False
	if _sigmask is None:return _popen(argv,started,grouped,handover,**kwargs)
	old_mask=_sigmask(signal.SIG_BLOCK,());block,go=_TTY_SIGNALS,b'1'
	if handover is not None and signal.SIGINT not in old_mask:block,go=_TTY_SIGNALS|{signal.SIGINT},b'2'
	try:_sigmask(signal.SIG_BLOCK,block);command=_popen(argv,started,grouped,handover,go,**kwargs);_sigmask(signal.SIG_SETMASK,old_mask)
	finally:_sigmask(signal.SIG_SETMASK,old_mask)
	return command
def _stdout_isatty():
	try:return sys.stdout.isatty()
	except Exception:return False
def _quote_command(command):
	if isinstance(command,str):return command
	return' '.join(shlex.quote(str(arg))for arg in command)
def _dry_run_line(task):
	line=_quote_command(task.command)
	if task._env is None and not task._extra_env:return line
	if task._env is not None:words=['-i']+[shlex.quote(f"{k}={v}")for(k,v)in _child_env(task).items()]
	else:words=['-u '+shlex.quote(k)for(k,v)in task._extra_env.items()if v is None];words+=[shlex.quote(f"{k}={v}")for(k,v)in task._extra_env.items()if v is not None]
	return f"env {' '.join(words)} {line}"
def _last_output_line(task,limit=200):
	for line in reversed(task.stdout if task._pty else task.stderr):
		line=line.strip()
		if line:return line if len(line)<=limit else line[:limit-3]+'...'
	return''
def _decor(index,width,prefix,color):
	pre='[%*d] '%(width,index)if prefix else''
	if not color:return pre,'',''
	r,g,b=int_to_color(index*2654435761&16777215);return pre,f"[38;2;{r};{g};{b}m",'\x1b[0m'
def _resolve_color(color,concurrent):
	if color is not None:return bool(color)
	return concurrent and _stdout_isatty()and not os.environ.get('NO_COLOR')
def _register(proc,pgid):_live_procs[proc]=pgid
def _unregister(proc):
	if proc.poll()is None:return
	_live_procs.pop(proc,None)
def _send(proc,pgid,sig):
	try:
		if pgid is not None:
			try:os.killpg(pgid,sig);return
			except ProcessLookupError:pass
		if proc.poll()is None:proc.send_signal(sig)
	except OSError:pass
def _group_alive(proc,pgid):
	if pgid is None:return proc.poll()is None
	try:os.killpg(pgid,0)
	except ProcessLookupError:return proc.poll()is None
	except OSError:pass
	return True
def _wait_all(commands,timeout):
	deadline=time.monotonic()+timeout;delay=.0005
	while True:
		for(proc,_)in commands:
			if proc.poll()is None:break
		else:return True
		remaining=deadline-time.monotonic()
		if remaining<=0:return False
		delay=min(delay*2,remaining,.05);time.sleep(delay)
def _give_up_on(commands,quiet=False):
	for(proc,_)in commands:
		if proc.poll()is not None:continue
		_live_procs.pop(proc,None)
		if not quiet and not proc.__dict__.get('_multicmd_quiet'):_write_error(f"multiCMD: pid {proc.pid} not seen to exit 1 s after {'SIGKILL'if _IS_POSIX else'kill()'}: no longer tracked")
def _kill_commands(commands,first_signal,quiet=False):
	commands=list(commands)
	if not commands:return
	if not _IS_POSIX:
		for(proc,_)in commands:
			if proc.poll()is None:
				try:proc.terminate()
				except OSError:pass
		if not _wait_all(commands,1):
			for(proc,_)in commands:
				if proc.poll()is None:
					try:proc.kill()
					except OSError:pass
			if not _wait_all(commands,1):_give_up_on(commands,quiet)
		for(proc,_)in commands:_unregister(proc)
		return
	for(proc,pgid)in commands:_send(proc,pgid,first_signal)
	time.sleep(.01)
	for(proc,pgid)in commands:_send(proc,pgid,signal.SIGTERM);_send(proc,pgid,signal.SIGCONT)
	_wait_all(commands,1);leftovers=[(proc,pgid)for(proc,pgid)in commands if _group_alive(proc,pgid)]
	for(proc,pgid)in leftovers:_send(proc,pgid,signal.SIGKILL)
	if not _wait_all(leftovers,1):_give_up_on(leftovers,quiet)
	for(proc,_)in commands:_unregister(proc)
def kill_all(sig=None):commands=list(_live_procs.copy().items())+list(_draining.copy().items());_kill_commands(commands,signal.SIGTERM if sig is None else sig)
def _cut_by_exit(task):return _exiting and not task._at_exit and threading.current_thread()is not threading.main_thread()
_starting={}
_EXIT_START_WAIT=1.
def _kill_running_at_exit():
	global _exiting;_exiting=True;deadline=time.monotonic()+_EXIT_START_WAIT
	while _starting and time.monotonic()<deadline:time.sleep(.005)
	killed=set()
	while True:
		commands=[(proc,pgid)for(proc,pgid)in _live_procs.copy().items()if proc not in killed]
		if not commands:return
		killed.update(proc for(proc,_)in commands);_kill_commands(commands,signal.SIGTERM)
atexit.register(_kill_running_at_exit)
def _resolve_pty(pty):
	global _PTY_WARNED
	if not pty:return False
	if _IS_POSIX:return True
	if not _PTY_WARNED:_PTY_WARNED=True;_write_error('multiCMD: pty mode needs a POSIX system, using pipes')
	return False
def _open_pty(reserved=0):
	global _PTY_WARNED
	try:import pty as _pty_module;master,slave=_pty_module.openpty()
	except(ImportError,OSError)as e:
		if not _PTY_WARNED:_PTY_WARNED=True;_write_error(f"multiCMD: cannot open a pty ({e}), using pipes")
		return None,None
	try:import fcntl,struct,termios;attrs=termios.tcgetattr(slave);attrs[1]&=~termios.OPOST;termios.tcsetattr(slave,termios.TCSANOW,attrs);columns,rows=get_terminal_size()[:2];fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',rows,max(columns-reserved,20),0,0))
	except Exception:pass
	return master,slave
def _watch(task,proc,timeout):
	sleep_time=1e-07
	while proc.poll()is None:
		if _stops(task):return'stop'
		if timeout>0 and time.monotonic()-task._last_output>timeout:return'timeout'
		time.sleep(sleep_time)
		if sleep_time<.001:sleep_time*=2
def _join(thread,timeout=None):
	deadline=None if timeout is None else time.monotonic()+timeout
	while True:
		step=.1 if deadline is None else min(.1,deadline-time.monotonic());thread.join(max(0,step))
		if not thread.is_alive():return True
		if step<=0:return False
def _drain(task,readers,timeout):
	while _any_alive(readers):
		if _stops(task):return'stop'
		for reader in readers:_join(reader,.05)
		if timeout>0 and time.monotonic()-task._last_output>timeout and _any_alive(readers):return'timeout'
def _join_all(threads,limit):
	deadline=None if limit is None else time.monotonic()+limit
	for thread in threads:_join(thread,None if deadline is None else max(0,deadline-time.monotonic()))
def _snapshot(mapping,drop_empty=False):
	items=[]
	for key in list(mapping):
		if drop_empty and not key:continue
		try:items.append((key,mapping[key]))
		except KeyError:pass
	return items
def _env_text(value,what):
	if isinstance(value,str):return value
	if os.name!='nt'and isinstance(value,(bytes,os.PathLike)):
		try:return os.fsdecode(value)
		except TypeError:pass
	raise TypeError(f"{what} must be a str{''if os.name=='nt'else', bytes or os.PathLike'}, not {type(value).__name__}")
class _EnvSnapshot(collections.abc.Mapping):
	__slots__='_data',
	def __init__(self,data):self._data=data
	def __getitem__(self,key):return self._data[key]
	def __iter__(self):return iter(self._data)
	def __len__(self):return len(self._data)
	def __repr__(self):return'_EnvSnapshot(%r)'%(self._data,)
	def __reduce__(self):return _EnvSnapshot,(self._data,)
	def copy(self):return dict(self._data)
def _check_env(mapping,name,allow_none=False,drop_empty=False):
	if mapping is None:return
	if not isinstance(mapping,collections.abc.Mapping):raise TypeError(f"{name} must be a mapping of variable names to values, not {type(mapping).__name__}")
	env={}
	for(key,value)in _snapshot(mapping,drop_empty=drop_empty):
		key=_env_text(key,f"{name}: variable name")
		if not key or'\x00'in key or'='in key[os.name=='nt':]:raise ValueError(f"{name}: invalid variable name {key!r}")
		if value is not None or not allow_none:
			value=_env_text(value,f"{name}[{key!r}]"+(' ( or None )'if allow_none else''))
			if'\x00'in value:raise ValueError(f"{name}[{key!r}] holds a NUL character")
		if os.name!='nt':
			try:
				os.fsencode(key)
				if value is not None:os.fsencode(value)
			except UnicodeEncodeError as e:raise ValueError(f"{name}[{key!r}] cannot be encoded: {e}")from None
		env[key]=value
	return _EnvSnapshot(env)if env or not allow_none else None
def _check_pass_fds(pass_fds):
	if pass_fds is None:return()
	if isinstance(pass_fds,(str,bytes,bytearray)):raise TypeError(f"pass_fds must be a descriptor, an object with fileno() or an iterable of them, not {type(pass_fds).__name__}")
	if isinstance(pass_fds,int)or hasattr(pass_fds,'fileno'):pass_fds=pass_fds,
	try:items=list(pass_fds)
	except TypeError:raise TypeError(f"pass_fds must be a descriptor, an object with fileno() or an iterable of them, not {type(pass_fds).__name__}")from None
	if items and not _IS_POSIX:raise ValueError('pass_fds needs a POSIX system')
	fds=set()
	for item in items:
		fd=item
		if not isinstance(item,int)and hasattr(item,'fileno'):
			try:fd=item.fileno()
			except(OSError,ValueError)as e:raise ValueError(f"pass_fds: {item!r} has no open descriptor ( {e} )")from None
		if isinstance(fd,bool):raise ValueError(f"pass_fds: {item!r} is not a descriptor")
		if not isinstance(fd,int):raise TypeError(f"pass_fds: {item!r} is not a descriptor or an object with fileno()")
		if fd<0:raise ValueError(f"pass_fds: {fd} is not a descriptor")
		try:os.fstat(fd)
		except OSError as e:raise ValueError(f"pass_fds: descriptor {fd} is not open ( {e.strerror} )")from None
		fds.add(fd)
	return tuple(sorted(fds))
def _apply_extra_env(env,extra_env):
	names={key.upper():key for key in env}if os.name=='nt'else None
	for(key,value)in extra_env.items():
		if names is not None:
			old=names.pop(key.upper(),None)
			if old is not None:del env[old]
			if value is not None:names[key.upper()]=key
		if value is None:env.pop(key,None)
		else:env[key]=value
def _child_env(task,pager=False):
	base,extra=task._env,task._extra_env
	if base is None and extra is None and not pager:return
	if base is None:
		env=dict(_snapshot(os.environ,drop_empty=extra is not None))
		if pager:env.update(PAGER='cat',GIT_PAGER='cat')
	elif extra is None and not pager:return base.copy()
	else:
		env=base.copy()
		if pager:env.setdefault('PAGER','cat');env.setdefault('GIT_PAGER','cat')
	if extra:_apply_extra_env(env,extra)
	return env
def _stop_command(proc,pgid,quiet=False):_kill_commands([(proc,pgid)],signal.SIGINT,quiet)
def _execute(task,timeout,quiet,decor,use_pty=False):
	explicit=task._env is not None or task._extra_env is not None;master=None;proc=pgid=None;started=[];streams=[];readers=[];waiting=False;interrupted=None
	try:
		if use_pty:master,slave=_open_pty(len(decor[0]))
		if master is not None:
			try:proc,pgid=_spawn(task.command,slave,slave,_child_env(task,pager=True),explicit,started,task._pass_fds)
			finally:os.close(slave)
			task._pty=True;fd,master=master,None;streams=[(io.open(fd,'rb'),task.stdout)]
		else:proc,pgid=_spawn(task.command,subprocess.PIPE,subprocess.PIPE,_child_env(task),explicit,started,task._pass_fds);streams=[(proc.stdout,task.stdout),(proc.stderr,task.stderr)]
		proc._multicmd_quiet=quiet;_register(proc,pgid);_starting.pop(task,None)
		if _cut_by_exit(task):task.stop=True
		task._env=task._extra_env=None;task.start_time=time.time();task._last_output=time.monotonic();readers=[_Reader(_read_stream,(stream,target,task,decor,quiet))for(stream,target)in streams]
		for reader in readers:reader.start()
		waiting=True;reason=_watch(task,proc,timeout)
	except BaseException as e:
		if proc is None and started:proc,pgid=started[-1]
		if proc is not None:_live_procs[proc]=pgid;proc._multicmd_quiet=quiet
		_starting.pop(task,None)
		if master is not None:os.close(master)
		if proc is None:raise
		for(i,(stream,_))in enumerate(streams or[(proc.stdout,None),(proc.stderr,None)]):
			if stream is not None and(i>=len(readers)or isinstance(e,Exception)and not readers[i]._started.is_set()):stream.close()
		if isinstance(e,Exception)and not waiting:_stop_command(proc,pgid,quiet);_unregister(proc);_join_all(readers,1);raise
		reason,interrupted='stop',e
	draining=reason is None;marker_at=None
	try:
		if draining:_draining[proc]=pgid
		else:
			if reason=='timeout':marker_at=len(task.stderr)
			_stop_command(proc,pgid,quiet)
		_unregister(proc);task.returncode=proc.poll()
		if draining:
			try:reason=_drain(task,readers,timeout)
			except BaseException as e:reason,interrupted='stop',e
			if reason=='timeout':marker_at=len(task.stderr)
			if reason is not None:_stop_command(proc,pgid,quiet)
	except BaseException:
		if draining:_live_procs[proc]=pgid;_draining.pop(proc,None)
		_send(proc,pgid,signal.SIGTERM);_send(proc,pgid,signal.SIGCONT);raise
	_draining.pop(proc,None);_join_all(readers,None if reason is None else 1)
	if reason=='timeout':
		task.returncode=124
		if _any_alive(readers):task.stderr=list(task.stderr)
		task.stderr.insert(marker_at,'Timeout!')
	elif task.returncode is None:
		if task.stderr and task.stderr[-1].strip().startswith('Ctrl C detected, Emergency Stop!'):task.returncode=137
		else:task.returncode=-1;task._error=True
	task.end_time=time.time();task._finalized=True
	if interrupted is not None:raise interrupted
def __run_command(task,sem,timeout=60,quiet=False,dry_run=False,with_stdErr=False,decor=('','',''),use_pty=False):
	with sem:
		if _stops(task):task._env=task._extra_env=None;return task.stdout+task.stderr if with_stdErr else task.stdout
		try:
			_starting[task]=None
			if _cut_by_exit(task):raise RuntimeError('not started: the interpreter is exiting')
			if dry_run:
				if not quiet:_display.message(_dry_run_line(task))
				return task.stdout+task.stderr if with_stdErr else task.stdout
			if not quiet:_display.message('Running command: '+_quote_command(task.command),decor);_display.message('-'*100,decor)
			if _reader_quit():return task.stdout+task.stderr if with_stdErr else task.stdout
			_execute(task,timeout,quiet,decor,use_pty)
		except Exception as e:
			if task._finalized:raise
			if isinstance(e,FileNotFoundError):_cannot_run(task,127,'Command path not found',e,quiet)
			elif isinstance(e,(PermissionError,_ExecRefused)):_cannot_run(task,126,'Command cannot be executed',e,quiet)
			else:_fail_task(task,str(e),e,quiet)
		finally:_starting.pop(task,None);task._finalized=True;task._env=task._extra_env=None
		if not quiet:_display.message('-'*100,decor);_display.message(f"Process exited with return code {task.returncode}",decor)
		if with_stdErr:return task.stdout+task.stderr
		else:return task.stdout
def _write_error(text,end='\n'):
	if sys.stderr is None:return
	try:print(text,file=sys.stderr,end=end,flush=True)
	except Exception:pass
def _cannot_run(task,returncode,what,error,quiet=False):
	task.stderr.append(str(error));task.returncode=returncode
	if not quiet:_write_error(f"{what}: {task.command[0]}")
def _fail_task(task,message,error,quiet=False):
	lines=message.split('\n');task.stderr.extend(lines+''.join(traceback.format_exception(type(error),error,error.__traceback__)).split('\n'));task.returncode=-1;task._error=True;task.end_time=time.time();task._env=task._extra_env=None;task._finalized=True
	if not quiet:_write_error(f"Error running command: {task.command}\n"+'\n'.join(lines))
def _run_executor_task(task,permit,timeout,quiet,dry_run,finished,decor=('','',''),use_pty=False):
	task.thread=threading.current_thread()
	try:__run_command(task,permit,timeout,quiet,dry_run,decor=decor,use_pty=use_pty)
	finally:finished.append((threading.current_thread(),task))
def __run_tracked_command(task,sem,timeout,quiet,dry_run,decor=('','',''),use_pty=False):
	try:__run_command(task,sem,timeout,quiet,dry_run,decor=decor,use_pty=use_pty)
	finally:__finished_threads.append(threading.current_thread())
def __reap_finished_threads():
	while True:
		try:thread=__finished_threads.popleft()
		except IndexError:return
		try:thread.join()
		finally:__running_threads.discard(thread)
def _split_command(command):
	try:return shlex.split(command)
	except ValueError as e:raise ValueError(f"cannot parse command {command!r}: {e}")from None
def _shell_argv(command):
	if _IS_POSIX:
		if not isinstance(command,str):command=_quote_command(command)
		return['/bin/sh','-c',command]
	if not isinstance(command,str):raise ValueError(f"shell mode on this system needs a command string, not an argument list ( a POSIX shell is required ): {command!r}")
	return[os.environ.get('COMSPEC','cmd.exe'),'/c',command]
def _shell_template(template):
	if'{}'not in template:return template+' "$1"'
	out=[];quote=None;i=0
	while i<len(template):
		if template.startswith('{}',i):out.append('"$1"'if quote is None else'$1'if quote=='"'else'\'"$1"\'');i+=2;continue
		char=template[i]
		if char=='\\'and quote!="'"and i+1<len(template):out.append(template[i:i+2]);i+=2;continue
		if quote is None and char in'\'"':quote=char
		elif char==quote:quote=None
		out.append(char);i+=1
	return''.join(out)
def _build_tasks(commands,parse=False,use_sudo=...,shell=False,env=None,extra_env=None,pass_fds=()):
	global USE_SUDO;global SUDO_PATH
	if use_sudo is...:use_sudo=USE_SUDO
	if use_sudo and not SUDO_PATH:_write_error('sudo not found in PATH, cannot use sudo. ignoring it...');use_sudo=False
	variables={};formatedCommands=[]
	for command in commands:formatedCommands.extend(__format_command(command,expand=parse,shell=shell,variables=variables))
	if use_sudo:formatedCommands=[[SUDO_PATH]+command for command in formatedCommands]
	env=_check_env(env,'env',drop_empty=True);extra_env=_check_env(extra_env,'extra_env',allow_none=True);pass_fds=_check_pass_fds(pass_fds);tasks=[Task(command)for command in formatedCommands];at_exit=_exiting and threading.current_thread()is threading.main_thread();unread=[]
	for task in tasks:task._env=env;task._extra_env=extra_env;task._pass_fds=pass_fds;task._at_exit=at_exit;task._unread=unread
	return tasks,bool(use_sudo)
def _validate_sudo():
	try:subprocess.run([SUDO_PATH,'-v'])
	except OSError as e:_write_error(f"sudo validation failed: {e}")
def __format_command(command,expand=False,shell=False,variables=None):
	if variables is None:variables={}
	if isinstance(command,str):
		if expand:commands=_expand_ranges_fast(command,variables)
		else:commands=[command]
		if shell:return[_shell_argv(command)for command in commands]
		return[_split_command(command)for command in commands]
	elif hasattr(command,'__iter__'):
		sanitized_command=[]
		for field in command:
			if isinstance(field,str):sanitized_command.append(field)
			else:sanitized_command.append(repr(field))
		if not expand:commands=[sanitized_command]
		else:sanitized_expanded_command=[_expand_ranges_fast(field,variables)for field in sanitized_command];commands=[list(command)for command in itertools.product(*sanitized_expanded_command)]
		if shell:return[_shell_argv(command)for command in commands]
		return commands
	else:return __format_command(str(command),expand=expand,shell=shell,variables=variables)
def ping(hosts,timeout=1,max_threads=0,quiet=True,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,wait_for_return=True,return_true_false=True):
	count='-c'if _IS_POSIX else'-n';single_host=False
	if isinstance(hosts,str):commands=[f"ping {count} 1 {hosts}"];single_host=True
	else:commands=[f"ping {count} 1 {host}"for host in hosts]
	if return_true_false:return_code_only=True
	results=run_commands(commands,timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,with_stdErr=with_stdErr,return_code_only=return_code_only,return_object=return_object,wait_for_return=wait_for_return)
	if return_true_false:
		if single_host:return not results[0]
		else:return[not result for result in results]
	elif single_host:return results[0]
	else:return results
def _stop_tasks(tasks):
	for task in tasks:task.stop=True
def _run_pool(tasks,decors,sem,timeout,quiet,dry_run,max_threads,use_pty=False):
	if not tasks:return
	pending=iter(list(zip(tasks,decors)));lock=threading.Lock()
	def worker():
		while True:
			with lock:
				try:task,decor=next(pending)
				except StopIteration:return
			task.thread=threading.current_thread()
			try:__run_command(task,sem,timeout,quiet,dry_run,decor=decor,use_pty=use_pty)
			finally:task.thread=None
	wanted=min(max_threads,len(tasks));workers=[_Thread(worker)for _ in range(wanted)]
	try:
		running,error,failed_last=[],None,False
		for thread in workers:
			try:thread.start()
			except Exception as e:
				if thread._started.is_set():raise
				error=e
				if failed_last:break
				failed_last=True
			else:running.append(thread);failed_last=False
		if not running:
			for task in tasks:_fail_task(task,f"cannot start a thread to run it: {error}",error,quiet)
			return
		if error is not None and not quiet:_write_error(f"multiCMD: started {len(running)} of {wanted} threads: {error}")
		for thread in running:
			while not _join(thread,.1):_reader_quit()
	except BaseException:_stop_tasks(tasks);_join_all(workers,5);raise
def run_command(command,timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,wait_for_return=True,sem=None,use_sudo=...,raise_error=False,prefix=False,color=None,pty=False,shell=False,env=None,extra_env=None,pass_fds=()):return run_commands(commands=[command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,with_stdErr=with_stdErr,return_code_only=return_code_only,return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem,use_sudo=use_sudo,raise_error=raise_error,prefix=prefix,color=color,pty=pty,shell=shell,env=env,extra_env=extra_env,pass_fds=pass_fds)[0]
def run_commands(commands,timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,parse=False,wait_for_return=True,sem=None,use_sudo=...,raise_error=False,prefix=False,color=None,pty=False,shell=False,env=None,extra_env=None,pass_fds=()):
	tasks,use_sudo=_build_tasks(commands,parse=parse,use_sudo=use_sudo,shell=shell,env=env,extra_env=extra_env,pass_fds=pass_fds)
	if max_threads<1:max_threads=len(tasks)
	concurrent=len(tasks)>1 and max_threads!=1;use_color=_resolve_color(color,concurrent);width=len(str(len(tasks)));decors=[_decor(index,width,prefix and concurrent,use_color)for index in range(1,len(tasks)+1)];use_pty=_resolve_pty(pty)
	if not sem:sem=threading.Semaphore(max_threads)
	if use_sudo and not dry_run:_validate_sudo()
	if dry_run:
		for task in tasks:
			if not quiet and not task._unread:
				_display.message(_dry_run_line(task))
				if _display.closed:task._unread.append(True)
			task._env=task._extra_env=None
	elif not wait_for_return:
		__reap_finished_threads();threads=[_Thread(__run_tracked_command,(task,sem,timeout,quiet,False,decor,use_pty),task)for(task,decor)in zip(tasks,decors)]
		try:
			for thread in threads:thread.task.thread=thread
			__running_threads.update(threads)
			for thread in threads:
				try:thread.start()
				except Exception as e:
					if thread._started.is_set():raise
					__running_threads.discard(thread);_fail_task(thread.task,f"cannot start its thread: {e}",e,quiet)
		except BaseException:
			_stop_tasks(tasks)
			for thread in threads:
				if not thread._started.is_set():__running_threads.discard(thread);thread.task._env=thread.task._extra_env=None
			_join_all(threads,5);raise
	elif max_threads>1:_run_pool(tasks,decors,sem,timeout,quiet,False,max_threads,use_pty)
	else:
		try:
			for(task,decor)in zip(tasks,decors):__run_command(task,sem,timeout,quiet,False,decor=decor,use_pty=use_pty)
		except BaseException:_stop_tasks(tasks);raise
	if wait_for_return and(tasks and tasks[0]._unread or _reader_quit()):raise BrokenPipeError(errno.EPIPE,'the reader of stdout quit')
	if raise_error:
		failed=[task for task in tasks if task.returncode not in(0,None)]
		if failed:raise CommandError(failed,total=len(tasks))
	if return_code_only:return[task.returncode for task in tasks]
	elif return_object:return tasks
	elif with_stdErr:return[task.stdout+task.stderr for task in tasks]
	else:return[task.stdout for task in tasks]
def join_threads(threads=...,timeout=None):
	if threads is...:threads=list(__running_threads.copy())
	else:threads=list(threads)
	_check_started(threads)
	try:
		for thread in threads:_join(thread,timeout)
	except BaseException:ours=[thread for thread in threads if isinstance(thread,_Thread)and thread.task is not None];_stop_tasks([thread.task for thread in ours]);_join_all(ours,5);raise
	__reap_finished_threads()
_EXIT_STATUS_HELP='exit status:\n  0      every command succeeded, or --dry-run\n  N      one command: its own exit code ( killed by signal S: 128+S )\n  N      several commands: how many failed, capped at 101\n  2      usage error\n  130    interrupted with Ctrl-C ( 128+S after SIGTERM / SIGHUP )\n  141    stdout closed ( `mcmd ... | head` ): commands stopped, ended by SIGPIPE ( 1 on Windows )'
def _exit_status(tasks):
	if len(tasks)==1:
		returncode=tasks[0].returncode
		if returncode is None or tasks[0]._error:return 1
		return 128-returncode if returncode<0 else returncode
	return min(sum(1 for task in tasks if task.returncode not in(0,None)),101)
def _install_cli_signal_handlers():
	def handler(signum,frame):kill_all(signum);sys.exit(128+signum)
	for name in('SIGTERM','SIGHUP'):
		signum=getattr(signal,name,None)
		if signum is not None and signal.getsignal(signum)!=signal.SIG_IGN:signal.signal(signum,handler)
	for signum in _TTY_SIGNALS:signal.signal(signum,signal.SIG_IGN)
def _point_at_devnull(stream):
	try:
		devnull=os.open(os.devnull,os.O_WRONLY)
		try:os.dup2(devnull,stream.fileno())
		finally:os.close(devnull)
	except(OSError,ValueError):pass
def _end_by_closed_stdout():
	_kill_running_at_exit()
	try:sys.stderr.flush()
	except Exception:pass
	sigpipe=getattr(signal,'SIGPIPE',None)
	if sigpipe is None:os._exit(1)
	signal.signal(sigpipe,signal.SIG_DFL);os.kill(os.getpid(),sigpipe);os._exit(128+sigpipe)
_RETURNCODE_LABELS={124:'timeout',126:'cannot execute',127:'not found'}
def _format_returncode(task):
	returncode=task.returncode
	if returncode is None:return'not run'
	if task._error:return f"{returncode} (error)"
	if returncode<0:
		try:name=signal.Signals(-returncode).name
		except ValueError:name=f"signal {-returncode}"
		return f"{returncode} ({name})"
	if returncode in _RETURNCODE_LABELS:return f"{returncode} ({_RETURNCODE_LABELS[returncode]})"
	return str(returncode)
def _summary(tasks,show_all=False):
	failed=[index for(index,task)in enumerate(tasks,1)if task.returncode not in(0,None)];rows=range(1,len(tasks)+1)if show_all else failed;table=''
	if rows:
		data=[['#','Command','Return code','Time']]
		for index in rows:task=tasks[index-1];duration=task.duration;data.append([str(index),_quote_command(task.command),_format_returncode(task),'-'if duration is None else f"{duration:.2f}s"])
		table=pretty_format_table(data)
	if failed:tally=f"{len(tasks)} commands: {len(tasks)-len(failed)} succeeded, {len(failed)} failed"
	else:tally=f"{len(tasks)} commands: all succeeded"
	return table+tally
def _read_input(path):
	if path=='-':return sys.stdin.read().splitlines()
	with open(path)as f:return f.read().splitlines()
def _fill_templates(templates,lines,shell=False):
	if shell and not _IS_POSIX:raise ValueError('-a with --shell needs a POSIX shell')
	commands=[]
	for template in templates:
		script=_shell_template(template)if shell else None;argv=None if shell else _split_command(template)
		for line in lines:
			if shell:commands.append(['/bin/sh','-c',script,'mcmd',line])
			elif any('{}'in arg for arg in argv):commands.append([arg.replace('{}',line)for arg in argv])
			else:commands.append(argv+[line])
	return commands
def _cli_commands(args):
	variables={}
	def expand(command):return _expand_ranges_fast(command,variables)if args.parse else[command]
	if args.arg_file:
		lines=[]
		for path in args.arg_file:lines.extend(line for line in _read_input(path)if line.strip())
		templates=[template for command in args.commands for template in expand(command)];commands=_fill_templates(templates,lines,args.shell)
	else:commands=[expanded for command in args.commands for expanded in expand(command)]
	for path in args.file or[]:
		for line in _read_input(path):
			if line.strip()and not line.lstrip().startswith('#'):commands.extend(expand(line))
	for command in commands:
		if isinstance(command,str)and not command.strip():raise ValueError('empty command')
	if args.shell:commands=[_shell_argv(command)if isinstance(command,str)else command for command in commands]
	return commands
def main():
	if sys.stderr is None:sys.stderr=open(os.devnull,'w')
	_display.cli=True;_display.watch=_watch_stdout()
	try:status=_run_cli()
	finally:
		_display.flush()
		if _display.closed:_end_by_closed_stdout()
		if _display.lost:_point_at_devnull(sys.stdout)
		_flush_stderr();_display.cli=False;_display.watch=None
	return status
def _flush_stderr():
	try:sys.stderr.flush()
	except(OSError,ValueError):_point_at_devnull(sys.stderr)
class _Parser(argparse.ArgumentParser):
	def error(self,message):_write_error(self.format_usage()+f"{self.prog}: error: {message}");sys.exit(2)
def _run_cli():
	parser=_Parser(description='Run multiple commands in parallel',epilog=_EXIT_STATUS_HELP,formatter_class=argparse.RawDescriptionHelpFormatter);parser.add_argument('commands',metavar='command',type=str,nargs='*',help='commands to run ( with -a: templates, {} is replaced by each input line )');parser.add_argument('-p','--parse',action='store_true',help='Parse ranged input and expand them into multiple commands');parser.add_argument('-t','--timeout',metavar='SECONDS',type=float,default=0,help='kill a command after SECONDS with no output at all ( 0 = never, the default )');parser.add_argument('-m','--max_threads',metavar='max_threads',type=int,default=1,help='maximum number of commands to run at once ( 0 = all )');parser.add_argument('--sudo',action='store_true',help='use sudo for commands');parser.add_argument('-q','--quiet',action='store_true',help='quiet mode: hide command output ( the summary still shows )');parser.add_argument('-n','--dry-run',action='store_true',help='print each command, quoted, instead of running it');parser.add_argument('--shell',action='store_true',help='run each command through /bin/sh -c ( pipes, ;, > and globs work )');parser.add_argument('-e','--env',metavar='NAME=VALUE',action='append',help='set NAME to VALUE in the environment of every command ( repeatable ); NAME alone removes it');parser.add_argument('-f','--file',metavar='FILE',action='append',help='read commands from FILE, one per line ( - for stdin; blank lines and # comments are skipped )');parser.add_argument('-a','--arg-file',metavar='FILE',action='append',help='run each command template once per line of FILE, with {} replaced by the line ( - for stdin )');summary=parser.add_mutually_exclusive_group();summary.add_argument('--summary',dest='summary',action='store_const',const='all',help='list every command in the end-of-run summary ( default: only failed ones )');summary.add_argument('--no-summary',dest='summary',action='store_const',const='none',help='do not print the end-of-run summary');parser.add_argument('--no-prefix',action='store_true',help='do not prefix output lines with [#] when commands run at once');parser.add_argument('--no-color',action='store_true',help='do not color output lines per command');pty_mode=parser.add_mutually_exclusive_group();pty_mode.add_argument('--pty',dest='pty',action='store_true',help='give commands a pseudo-terminal so they print live ( default when stdout is a terminal )');pty_mode.add_argument('--no-pty',dest='pty',action='store_false',help='give commands plain pipes');parser.set_defaults(summary='failed',pty=None);parser.add_argument('-V','--version',action='version',version=f"%(prog)s {version} @ {COMMIT_DATE} by pan@zopyr.us");args=parser.parse_args()
	if not args.commands and not args.file:parser.error('no commands given')
	if args.arg_file and not args.commands:parser.error('-a/--arg-file needs at least one command template')
	if'-'in(args.file or[])and'-'in(args.arg_file or[]):parser.error('-f - and -a - cannot both read stdin')
	extra_env={}
	for assignment in args.env or[]:
		name,equals,value=assignment.partition('=')
		if not name:parser.error(f"-e/--env: invalid assignment {assignment!r}")
		extra_env[name]=value if equals else None
	set_sudo(args.sudo);_install_cli_signal_handlers()
	try:
		commands=_cli_commands(args)
		if not commands:_write_error('mcmd: no commands to run');return 2
		use_pty=args.pty if args.pty is not None else _IS_POSIX and _stdout_isatty()and not args.quiet and not args.dry_run;tasks=run_commands(commands,timeout=args.timeout,max_threads=args.max_threads,quiet=args.quiet and not args.dry_run,dry_run=args.dry_run,with_stdErr=True,return_object=True,prefix=not args.no_prefix,color=False if args.no_color else None,pty=use_pty,extra_env=extra_env or None)
	except(OSError,ValueError)as e:
		if _display.closed:raise
		_write_error(f"mcmd: {e}");return 2
	except KeyboardInterrupt:_write_error('\nInterrupted');return 130
	if args.dry_run:return 0
	if len(tasks)>1 and args.summary!='none':_display.message(_summary(tasks,show_all=args.summary=='all'))
	return _exit_status(tasks)
def input_with_timeout_and_countdown(timeout,prompt='Please enter your selection'):
	_display.flush();_write_error(f"{prompt} [{timeout}s]: ",end='')
	try:import msvcrt
	except ImportError:msvcrt=None
	for remaining in range(timeout,0,-1):
		if msvcrt is not None:
			if msvcrt.kbhit():return input().strip()
		elif sys.stdin in select.select([sys.stdin],[],[],0)[0]:return input().strip()
		_write_error(f"\r{prompt} [{remaining}s]: ",end='');time.sleep(1)
def pretty_format_table(data,delimiter=None,header=None,full=False,remove_empty_columns=False):
	import re,unicodedata;version=1.21;_=version
	if not data:return''
	if isinstance(data,str):data=data.strip('\n').split('\n');data=[line.split(delimiter)for line in data]
	elif isinstance(data,dict):
		if isinstance(next(iter(data.values())),dict):
			if not header:header=['key']+list(next(iter(data.values())).keys())
			data=[[key]+list(value.values())for(key,value)in data.items()]
		else:data=[[key]+list(value)for(key,value)in data.items()]
	elif not isinstance(data,list):data=list(data)
	if isinstance(data[0],dict):
		if not header:header=list(data[0].keys())
		data=[list(item.values())for item in data]
	elif isinstance(data[0],str):data=[row.split(delimiter)for row in data]
	data=[[str(item)for item in row]for row in data]
	if isinstance(header,str):header=header.split(delimiter)
	if not header or not any(header):header=data[0];data=data[1:]
	num_cols=len(header);ansi=re.compile('(\\x1b\\[[0-?]*[ -/]*[@-~])')
	def char_width(ch):
		if ch<'̀':return 1
		if unicodedata.combining(ch)or unicodedata.category(ch)in('Mn','Me','Cf')or'ᅠ'<=ch<='ᇿ':return 0
		return 2 if unicodedata.east_asian_width(ch)in('W','F')else 1
	def visible_len(s):
		if'\x1b'in s:s=ansi.sub('',s)
		if not s or max(s)<'̀':return len(s)
		return sum(char_width(ch)for ch in s)
	def pad(s,width):return s+' '*(width-visible_len(s))
	def cut(s,width):
		used,out=0,[]
		for(i,piece)in enumerate(ansi.split(s)):
			if i%2:out.append(piece);continue
			for ch in piece:
				w=char_width(ch)
				if used+w>width:width=-1;break
				out.append(ch);used+=w
		return''.join(out)
	col_widths=[0]*num_cols
	for row in data:
		for(cwi,item)in enumerate(row[:num_cols]):
			vl=visible_len(item)
			if vl>col_widths[cwi]:col_widths[cwi]=vl
	cols_to_remove=[]
	if remove_empty_columns and not full:cols_to_remove=[i for(i,col_width)in enumerate(col_widths)if col_width==0];header=[item for(i,item)in enumerate(header)if i not in cols_to_remove];col_widths=[w for(i,w)in enumerate(col_widths)if i not in cols_to_remove];num_cols=len(header)
	header_widths=[visible_len(h)for h in header];col_widths=[max(w,hw)for(w,hw)in zip(col_widths,header_widths)];header=[pad(item,col_width)for(item,col_width)in zip(header,col_widths)];normalized_padded_data=[]
	for row in data:
		if cols_to_remove:row=[item for(i,item)in enumerate(row)if i not in cols_to_remove]
		if not any(row):row=['-'*col_width for col_width in col_widths]
		else:row=[pad(row[i]if i<len(row)else'',col_widths[i])for i in range(num_cols)]
		normalized_padded_data.append(row)
	data=normalized_padded_data;column_separator=' | ';horizontal_separator='-+-';terminal_width=get_terminal_size()[0]
	def table_width(col_widths,sep_len):return sum(col_widths)+sep_len*(len(col_widths)-1)
	def render(header,rows,column_widths,column_separator,horizontal_separator):return'\n'.join([column_separator.join(header),horizontal_separator.join('-'*width for width in column_widths),*(column_separator.join(row)for row in rows)])+'\n'
	if full or table_width(col_widths,len(column_separator))<=terminal_width:return render(header,data,col_widths,column_separator,horizontal_separator)
	column_separator='|';horizontal_separator='+'
	if table_width(col_widths,len(column_separator))<=terminal_width:return render(header,data,col_widths,column_separator,horizontal_separator)
	width_diff=[max(col_width-header_width,0)for(col_width,header_width)in zip(col_widths,header_widths)];total_overflow_width=table_width(col_widths,len(column_separator))-terminal_width
	for(i,diff)in sorted(enumerate(width_diff),key=lambda x:-x[1]):
		if total_overflow_width<=0:break
		if diff<=0:continue
		reduce_by=min(diff,total_overflow_width);col_widths[i]-=reduce_by;total_overflow_width-=reduce_by
	def truncate_to_width(string,width):
		if visible_len(string)<=width:return string
		stripped=string.rstrip()
		if visible_len(stripped)<=width:return pad(stripped,width)
		if width<2:return'.'[:width]
		return pad(cut(string,width-2)+'..',width)
	data=[[truncate_to_width(item,col_width)for(item,col_width)in zip(row,col_widths)]for row in data];header=[truncate_to_width(item,col_width)for(item,col_width)in zip(header,col_widths)];return render(header,data,col_widths,column_separator,horizontal_separator)
def parseTable(data,sort=False,min_space=2):
	if isinstance(data,str):data=data.strip('\n').split('\n')
	header_line=data[0];pattern='(\\S(?:.*?\\S)?)(?=\\s{'+str(min_space)+',}|\\s*$)';matches=list(re.finditer(pattern,header_line));data_list=[[]];columns=[]
	for(i,match)in enumerate(matches):
		col_name=match.group(1);data_list[0].append(col_name);start=match.start()
		if i+1<len(matches):end=matches[i+1].start()
		else:end=None
		columns.append((col_name,start,end))
	for line in data[1:]:
		if not line.strip():continue
		row=[]
		for(col_name,start,end)in columns:
			if end is not None:value=line[start:end].strip()
			else:value=line[start:].strip()
			row.append(value)
		data_list.append(row)
	if sort:data_list[1:]=sorted(data_list[1:],key=lambda x:x[0])
	return data_list
def slugify(value,allow_unicode=False):
	import unicodedata;value=str(value)
	if allow_unicode:value=unicodedata.normalize('NFKC',value)
	else:value=unicodedata.normalize('NFKD',value).encode('ascii','ignore').decode('ascii')
	value=re.sub('[^\\w\\s-]','',value.lower());return re.sub('[-\\s]+','-',value).strip('-_')
def get_terminal_size():
	try:import os;_tsize=os.get_terminal_size()
	except Exception:
		try:import fcntl,struct,termios;packed=fcntl.ioctl(0,termios.TIOCGWINSZ,struct.pack('HHHH',0,0,0,0));rows,columns=struct.unpack('HHHH',packed)[:2];_tsize=columns,rows
		except Exception:import shutil;_tsize=shutil.get_terminal_size(fallback=(240,50))
	return _tsize
_BAR_BLOCKS='▁▂▃▄▅▆▇█'
_ASCII_BAR=str.maketrans(_BAR_BLOCKS,'.:-=+*%#')
def _genrate_progress_bar(iteration,total,prefix='',suffix='',columns=240):
	noPrefix=False;noSuffix=False;noPercent=False;noBar=False
	if total==0:return f"{prefix} iteration:{iteration} {suffix}".ljust(columns)
	percent=f"|{'{0:.1f}'.format(100*(iteration/float(total)))}% ";length=columns-len(prefix)-len(suffix)-len(percent)-3
	if length<=0:length=columns-len(prefix)-len(suffix)-3;noPercent=True
	if length<=0:length=columns-len(suffix)-3;noPrefix=True
	if length<=0:length=columns-3;noSuffix=True
	if length<=0:return f"""{prefix}
iteration:
 {iteration}
total:
 {total}
| {suffix}
"""
	if iteration==0:noBar=True
	filled_length=int(length*iteration//total);progress_chars=_BAR_BLOCKS;fractional_progress=length*iteration/total-filled_length;char_index=int(fractional_progress*(len(progress_chars)-1));bar_char=progress_chars[char_index]
	if filled_length==length:bar=progress_chars[-1]*length
	else:bar=progress_chars[-1]*filled_length+bar_char+'_'*(length-filled_length)
	lineOut=''
	if not noPrefix:lineOut+=prefix
	if not noBar:
		lineOut+=f"{bar}"
		if not noPercent:lineOut+=percent
	elif length>=16:lineOut+=' Calculating... '
	if not noSuffix:lineOut+=suffix
	return lineOut
def _writable(text,stream):
	encoding=getattr(stream,'encoding',None)
	if not encoding:return text
	try:text.encode(encoding,getattr(stream,'errors',None)or'strict');return text
	except UnicodeEncodeError:pass
	except LookupError:return text
	try:_BAR_BLOCKS.encode(encoding)
	except UnicodeEncodeError:text=text.translate(_ASCII_BAR)
	return text.encode(encoding,'replace').decode(encoding)
def print_progress_bar(iteration,total,prefix='',suffix=''):
	prefix+=' |'if not prefix.endswith(' |')else'';suffix=f"| {suffix}"if not suffix.startswith('| ')else suffix
	try:
		columns,_=get_terminal_size();_display._write(_writable(f"\r{_genrate_progress_bar(iteration,total,prefix,suffix,columns)}",sys.stdout))
		if iteration==total and total>0:_display._write('\n')
	except Exception:
		if iteration%5==0:_display._write(_writable(_genrate_progress_bar(iteration,total,prefix,suffix),sys.stdout)+'\n')
def format_bytes(size,use_1024_bytes=None,to_int=False,to_str=False,str_format='.2f'):
	if to_int or isinstance(size,str):
		if isinstance(size,int):return size
		elif isinstance(size,str):
			match=re.match('(\\d+(\\.\\d+)?)\\s*([a-zA-Z]*)',size)
			if not match:
				if to_str:return size
				_write_error(f"Invalid size format. Expected format: 'number [unit]', e.g., '1.5 GiB' or '1.5GiB'\nGot: {size}");return 0
			number,_,unit=match.groups();number=float(number);unit=unit.strip().lower().rstrip('b')
			if unit.endswith('i'):use_1024_bytes=True
			elif use_1024_bytes is None:use_1024_bytes=False
			unit=unit.rstrip('i')
			if use_1024_bytes:power=2**10
			else:power=10**3
			unit_labels={'':0,'k':1,'m':2,'g':3,'t':4,'p':5,'e':6,'z':7,'y':8}
			if unit not in unit_labels:
				if to_str:return size
			else:
				if to_str:return format_bytes(size=int(number*power**unit_labels[unit]),use_1024_bytes=use_1024_bytes,to_str=True,str_format=str_format)
				return int(number*power**unit_labels[unit])
		else:
			try:return int(size)
			except Exception:return 0
	elif to_str or isinstance(size,int)or isinstance(size,float):
		if isinstance(size,str):
			try:size=size.rstrip('B').rstrip('b');size=float(size.lower().strip())
			except Exception:return size
		if use_1024_bytes or use_1024_bytes is None:
			power=2**10;n=0;power_labels={0:'',1:'Ki',2:'Mi',3:'Gi',4:'Ti',5:'Pi',6:'Ei',7:'Zi',8:'Yi'}
			while size>=power:size/=power;n+=1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace('  ',' ')
		else:
			power=10**3;n=0;power_labels={0:'',1:'K',2:'M',3:'G',4:'T',5:'P',6:'E',7:'Z',8:'Y'}
			while size>=power:size/=power;n+=1
			return f"{size:{str_format}} {' '}{power_labels[n]}".replace('  ',' ')
	else:
		try:return format_bytes(float(size),use_1024_bytes)
		except Exception:pass
		return 0
if __name__=='__main__':sys.exit(main())