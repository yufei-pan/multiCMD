import time,threading,io,sys,subprocess,select,string,re,itertools,signal
if True:
	class multiCMD:
		version='1.35_min'
		__version__=version
		COMMIT_DATE='2025-09-10'
		__running_threads=set()
		__variables={}
		_BRACKET_RX=re.compile('\\[([^\\]]+)\\]')
		_ALPHANUM=string.digits+string.ascii_letters
		_ALPHA_IDX={B:A for(A,B)in enumerate(_ALPHANUM)}
		class Task:
			def __init__(A,command):A.command=command;A.returncode=None;A.stdout=[];A.stderr=[];A.thread=None;A.stop=False
			def __iter__(A):return zip(['command','returncode','stdout','stderr'],[A.command,A.returncode,A.stdout,A.stderr])
			def __repr__(A):return f"Task(command={A.command}, returncode={A.returncode}, stdout={A.stdout}, stderr={A.stderr}, stop={A.stop})"
			def __str__(A):return str(dict(A))
			def is_alive(A):
				if A.thread is not None:return A.thread.is_alive()
				return False
		class AsyncExecutor:
			def __init__(A,max_threads=1,semaphore=...,timeout=0,quiet=True,dry_run=False,parse=False):
				C=max_threads;B=semaphore;A.max_threads=C
				if B is...:B=threading.Semaphore(C)
				A.semaphore=B;A.runningThreads=[];A.tasks=[];A.timeout=timeout;A.quiet=quiet;A.dry_run=dry_run;A.parse=parse;A.__lastNotJoined=0
			def __iter__(A):return iter(A.tasks)
			def __repr__(A):return f"AsyncExecutor(max_threads={A.max_threads}, semaphore={A.semaphore}, runningThreads={A.runningThreads}, tasks={A.tasks}, timeout={A.timeout}, quiet={A.quiet}, dry_run={A.dry_run}, parse={A.parse})"
			def __str__(A):return str(A.tasks)
			def __len__(A):return len(A.tasks)
			def __bool__(A):return bool(A.tasks)
			def run_commands(A,commands,timeout=...,max_threads=...,quiet=...,dry_run=...,parse=...,sem=...):
				G=sem;F=parse;E=dry_run;D=quiet;C=max_threads;B=timeout
				if B is...:B=A.timeout
				if C is...:C=A.max_threads
				if D is...:D=A.quiet
				if E is...:E=A.dry_run
				if F is...:F=A.parse
				if G is...:G=A.semaphore
				if len(A.runningThreads)>130000:
					A.wait(timeout=0)
					if len(A.runningThreads)>130000:
						print('The amount of running threads approching cpython limit of 130704. Waiting until some available.')
						while len(A.runningThreads)>120000:A.wait(timeout=1)
				elif len(A.runningThreads)+A.__lastNotJoined>1000:A.wait(timeout=0);A.__lastNotJoined=len(A.runningThreads)
				H=multiCMD.run_commands(commands,timeout=B,max_threads=C,quiet=D,dry_run=E,with_stdErr=False,return_code_only=False,return_object=True,parse=F,wait_for_return=False,sem=G);A.tasks.extend(H);A.runningThreads.extend([A.thread for A in H]);return H
			def run_command(A,command,timeout=...,max_threads=...,quiet=...,dry_run=...,parse=...,sem=...):return A.run_commands([command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,parse=parse,sem=sem)[0]
			def wait(A,timeout=...,threads=...):
				C=threads;B=timeout
				if C is...:C=A.runningThreads
				if B is...:B=A.timeout
				for D in C:
					if B>=0:D.join(timeout=B)
					else:D.join()
				A.runningThreads=[A for A in A.runningThreads if A.is_alive()];return A.runningThreads
			def stop(A,timeout=...):
				for B in A.tasks:B.stop=True
				A.wait(timeout);return A.tasks
			def cleanup(A,timeout=...):A.stop(timeout);A.tasks=[];A.runningThreads=[];return A.tasks
			def join(B,timeout=...,threads=...,print_error=True):
				B.wait(timeout=timeout,threads=threads)
				for A in B.tasks:
					if A.returncode!=0 and print_error:print(f"Command: {A.command} failed with return code: {A.returncode}");print('Stdout:');print('\n  '.join(A.stdout));print('Stderr:');print('\n  '.join(A.stderr))
				return B.tasks
			def get_results(A,with_stdErr=False):
				if with_stdErr:return[A.stdout+A.stderr for A in A.tasks]
				else:return[A.stdout for A in A.tasks]
			def get_return_codes(A):return[A.returncode for A in A.tasks]
		def _expand_piece(piece,vars_):
			D=vars_;C=piece;C=C.strip()
			if':'in C:E,F,G=C.partition(':');D[E]=G;return
			if'-'in C:
				A,F,B=(A.strip()for A in C.partition('-'));A=D.get(A,A);B=D.get(B,B)
				if A.isdigit()and B.isdigit():H=max(len(A),len(B));return[f"{A:0{H}d}"for A in range(int(A),int(B)+1)]
				if all(A in string.hexdigits for A in A+B):return[format(A,'x')for A in range(int(A,16),int(B,16)+1)]
				try:return[multiCMD._ALPHANUM[A]for A in range(multiCMD._ALPHA_IDX[A],multiCMD._ALPHA_IDX[B]+1)]
				except KeyError:pass
			return[D.get(C,C)]
		def _expand_ranges_fast(inStr):
			D=inStr;A=[];B=0
			for C in multiCMD._BRACKET_RX.finditer(D):
				if C.start()>B:A.append([D[B:C.start()]])
				E=[]
				for G in C.group(1).split(','):
					F=multiCMD._expand_piece(G,multiCMD.__variables)
					if F:E.extend(F)
				A.append(E or['']);B=C.end()
			A.append([D[B:]]);return[''.join(A)for A in itertools.product(*A)]
		def __handle_stream(stream,target,pre='',post='',quiet=False):
			E=quiet;C=target
			def D(current_line,target,keepLastLine=True):
				A=target
				if not keepLastLine:
					if not E:sys.stdout.write('\r')
					A.pop()
				elif not E:sys.stdout.write('\n')
				B=current_line.decode('utf-8',errors='backslashreplace');A.append(B)
				if not E:sys.stdout.write(pre+B+post);sys.stdout.flush()
			A=bytearray();B=True
			for F in iter(lambda:stream.read(1),b''):
				if F==b'\n':
					if not B and A:D(A,C,keepLastLine=False)
					elif B:D(A,C,keepLastLine=True)
					A=bytearray();B=True
				elif F==b'\r':D(A,C,keepLastLine=B);A=bytearray();B=False
				else:A.extend(F)
			if A:D(A,C,keepLastLine=B)
		def int_to_color(n,brightness_threshold=500):
			B=brightness_threshold;A=hash(str(n));C=A>>16&255;D=A>>8&255;E=A&255
			if C+D+E<B:return multiCMD.int_to_color(A,B)
			return C,D,E
		def __run_command(task,sem,timeout=60,quiet=False,dry_run=False,with_stdErr=False,identity=None):
			I=timeout;F=identity;E=quiet;A=task;C='';D=''
			with sem:
				try:
					if F is not None:
						if F==...:F=threading.get_ident()
						P,Q,R=multiCMD.int_to_color(F);C=f"[38;2;{P};{Q};{R}m";D='\x1b[0m'
					if not E:print(C+'Running command: '+' '.join(A.command)+D);print(C+'-'*100+D)
					if dry_run:return A.stdout+A.stderr
					B=subprocess.Popen(A.command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,stdin=subprocess.PIPE);J=threading.Thread(target=multiCMD.__handle_stream,args=(B.stdout,A.stdout,C,D,E),daemon=True);J.start();K=threading.Thread(target=multiCMD.__handle_stream,args=(B.stderr,A.stderr,C,D,E),daemon=True);K.start();L=time.time();M=len(A.stdout)+len(A.stderr);time.sleep(0);H=1e-07
					while B.poll()is None:
						if A.stop:B.send_signal(signal.SIGINT);time.sleep(.01);B.terminate();break
						if I>0:
							if len(A.stdout)+len(A.stderr)!=M:L=time.time();M=len(A.stdout)+len(A.stderr)
							elif time.time()-L>I:A.stderr.append('Timeout!');B.send_signal(signal.SIGINT);time.sleep(.01);B.terminate();break
						time.sleep(H)
						if H<.001:H*=2
					A.returncode=B.poll();J.join(timeout=1);K.join(timeout=1);N,O=B.communicate()
					if N:multiCMD.__handle_stream(io.BytesIO(N),A.stdout,A)
					if O:multiCMD.__handle_stream(io.BytesIO(O),A.stderr,A)
					if A.returncode is None:
						if A.stderr and A.stderr[-1].strip().startswith('Timeout!'):A.returncode=124
						elif A.stderr and A.stderr[-1].strip().startswith('Ctrl C detected, Emergency Stop!'):A.returncode=137
						else:A.returncode=-1
				except FileNotFoundError as G:print(f"Command / path not found: {A.command[0]}",file=sys.stderr,flush=True);A.stderr.append(str(G));A.returncode=127
				except Exception as G:import traceback as S;print(f"Error running command: {A.command}",file=sys.stderr,flush=True);print(str(G).split('\n'));A.stderr.extend(str(G).split('\n'));A.stderr.extend(S.format_exc().split('\n'));A.returncode=-1
				if not E:print(C+'\n'+'-'*100+D);print(C+f"Process exited with return code {A.returncode}"+D)
				if with_stdErr:return A.stdout+A.stderr
				else:return A.stdout
		def __format_command(command,expand=False):
			D=expand;A=command
			if isinstance(A,str):
				if D:B=multiCMD._expand_ranges_fast(A)
				else:B=[A]
				return[A.split()for A in B]
			elif hasattr(A,'__iter__'):
				C=[]
				for E in A:
					if isinstance(E,str):C.append(E)
					else:C.append(repr(E))
				if not D:return[C]
				F=[multiCMD._expand_ranges_fast(A)for A in C];B=list(itertools.product(*F));return[list(A)for A in B]
			else:return multiCMD.__format_command(str(A),expand=D)
		def ping(hosts,timeout=1,max_threads=0,quiet=True,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,wait_for_return=True,return_true_false=True):
			E=return_true_false;D=return_code_only;B=hosts;C=False
			if isinstance(B,str):F=[f"ping -c 1 {B}"];C=True
			else:F=[f"ping -c 1 {A}"for A in B]
			if E:D=True
			A=multiCMD.run_commands(F,timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,with_stdErr=with_stdErr,return_code_only=D,return_object=return_object,wait_for_return=wait_for_return)
			if E:
				if C:return not A[0]
				else:return[not A for A in A]
			elif C:return A[0]
			else:return A
		def run_command(command,timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,wait_for_return=True,sem=None):return multiCMD.run_commands(commands=[command],timeout=timeout,max_threads=max_threads,quiet=quiet,dry_run=dry_run,with_stdErr=with_stdErr,return_code_only=return_code_only,return_object=return_object,parse=False,wait_for_return=wait_for_return,sem=sem)[0]
		def run_commands(commands,timeout=0,max_threads=1,quiet=False,dry_run=False,with_stdErr=False,return_code_only=False,return_object=False,parse=False,wait_for_return=True,sem=None):
			K=wait_for_return;J=dry_run;I=quiet;H=timeout;C=max_threads;B=sem;E=[]
			for L in commands:E.extend(multiCMD.__format_command(L,expand=parse))
			A=[multiCMD.Task(A)for A in E]
			if C<1:C=len(E)
			if C>1 or not K:
				if not B:B=threading.Semaphore(C)
				F=[threading.Thread(target=multiCMD.__run_command,args=(A,B,H,I,J,...),daemon=True)for A in A]
				for(D,G)in zip(F,A):G.thread=D;D.start()
				if K:
					for D in F:D.join()
				else:multiCMD.__running_threads.update(F)
			else:
				B=threading.Semaphore(1)
				for G in A:multiCMD.__run_command(G,B,H,I,J,identity=None)
			if return_code_only:return[A.returncode for A in A]
			elif return_object:return A
			elif with_stdErr:return[A.stdout+A.stderr for A in A]
			else:return[A.stdout for A in A]
		def join_threads(threads=__running_threads,timeout=None):
			A=threads
			for B in A:B.join(timeout=timeout)
			if A is multiCMD.__running_threads:multiCMD.__running_threads={A for A in A if A.is_alive()}
		def input_with_timeout_and_countdown(timeout,prompt='Please enter your selection'):
			B=prompt;A=timeout;print(f"{B} [{A}s]: ",end='',flush=True)
			for C in range(A,0,-1):
				if sys.stdin in select.select([sys.stdin],[],[],0)[0]:return input().strip()
				print(f"\r{B} [{C}s]: ",end='',flush=True);time.sleep(1)
		def pretty_format_table(data,delimiter='\t',header=None,full=False):
			O=delimiter;B=header;A=data;import re;S=1.12;Z=S
			def J(s):return len(re.sub('\\x1b\\[[0-?]*[ -/]*[@-~]','',s))
			def L(col_widths,sep_len):A=col_widths;return sum(A)+sep_len*(len(A)-1)
			def T(s,width):
				A=width
				if J(s)<=A:return s
				if A<=0:return''
				return s[:max(A-2,0)]+'..'
			if not A:return''
			if isinstance(A,str):A=A.strip('\n').split('\n');A=[A.split(O)for A in A]
			elif isinstance(A,dict):
				if isinstance(next(iter(A.values())),dict):H=[['key']+list(next(iter(A.values())).keys())];H.extend([[A]+list(B.values())for(A,B)in A.items()]);A=H
				else:A=[[A]+list(B)for(A,B)in A.items()]
			elif not isinstance(A,list):A=list(A)
			if isinstance(A[0],dict):H=[list(A[0].keys())];H.extend([list(A.values())for A in A]);A=H
			A=[[str(A)for A in A]for A in A];C=len(A[0]);U=B is not None
			if not U:B=A[0];E=A[1:]
			else:
				if isinstance(B,str):B=B.split(O)
				if len(B)<C:B=B+['']*(C-len(B))
				elif len(B)>C:B=B[:C]
				E=A
			def V(hdr,rows_):
				B=hdr;C=[0]*len(B)
				for A in range(len(B)):C[A]=max(J(B[A]),*(J(B[A])for B in rows_ if A<len(B)))
				return C
			P=[]
			for F in E:
				if len(F)<C:F=F+['']*(C-len(F))
				elif len(F)>C:F=F[:C]
				P.append(F)
			E=P;D=V(B,E);G=' | ';I='-+-';M=multiCMD.get_terminal_size()[0]
			def K(hdr,rows,col_w,sep_str,hsep_str):
				D=hsep_str;C=col_w;E=sep_str.join('{{:<{}}}'.format(A)for A in C);A=[];A.append(E.format(*hdr));A.append(D.join('-'*A for A in C))
				for B in rows:
					if not any(B):A.append(D.join('-'*A for A in C))
					else:B=[T(B[A],C[A])for A in range(len(B))];A.append(E.format(*B))
				return'\n'.join(A)+'\n'
			if full:return K(B,E,D,G,I)
			if L(D,len(G))<=M:return K(B,E,D,G,I)
			G='|';I='+'
			if L(D,len(G))<=M:return K(B,E,D,G,I)
			W=[J(A)for A in B];X=[max(D[A]-W[A],0)for A in range(C)];N=L(D,len(G))-M
			for(Y,Q)in sorted(enumerate(X),key=lambda x:-x[1]):
				if N<=0:break
				if Q<=0:continue
				R=min(Q,N);D[Y]-=R;N-=R
			return K(B,E,D,G,I)
		def parseTable(data,sort=False):
			A=data
			if isinstance(A,str):A=A.strip('\n').split('\n')
			M=A[0];N='(\\S(?:.*?\\S)?)(?=\\s{2,}|\\s*$)';E=list(re.finditer(N,M));B=[[]];H=[]
			for(I,J)in enumerate(E):
				F=J.group(1);B[0].append(F);D=J.start()
				if I+1<len(E):C=E[I+1].start()
				else:C=None
				H.append((F,D,C))
			for G in A[1:]:
				if not G.strip():continue
				K=[]
				for(F,D,C)in H:
					if C is not None:L=G[D:C].strip()
					else:L=G[D:].strip()
					K.append(L)
				B.append(K)
			if sort:B[1:]=sorted(B[1:],key=lambda x:x[0])
			return B
		def slugify(value,allow_unicode=False):
			A=value;import unicodedata as B;A=str(A)
			if allow_unicode:A=B.normalize('NFKC',A)
			else:A=B.normalize('NFKD',A).encode('ascii','ignore').decode('ascii')
			A=re.sub('[^\\w\\s-]','',A.lower());return re.sub('[-\\s]+','-',A).strip('-_')
		def get_terminal_size():
			try:import os;A=os.get_terminal_size()
			except:
				try:import fcntl,termios as C,struct as B;D=fcntl.ioctl(0,C.TIOCGWINSZ,B.pack('HHHH',0,0,0,0));A=B.unpack('HHHH',D)[:2]
				except:import shutil as E;A=E.get_terminal_size(fallback=(120,30))
			return A
		def _genrate_progress_bar(iteration,total,prefix='',suffix='',columns=120):
			G=columns;F=prefix;E=total;C=suffix;B=iteration;J=False;K=False;L=False;M=False
			if E==0:return f"{F} iteration:{B} {C}".ljust(G)
			N=f"|{'{0:.1f}'.format(100*(B/float(E)))}% ";A=G-len(F)-len(C)-len(N)-3
			if A<=0:A=G-len(F)-len(C)-3;L=True
			if A<=0:A=G-len(C)-3;J=True
			if A<=0:A=G-3;K=True
			if A<=0:return f"""{F}
	iteration:
	{B}
	total:
	{E}
	| {C}
	"""
			if B==0:M=True
			H=int(A*B//E);I='▁▂▃▄▅▆▇█';P=A*B/E-H;Q=int(P*(len(I)-1));R=I[Q]
			if H==A:O=I[-1]*A
			else:O=I[-1]*H+R+'_'*(A-H)
			D=''
			if not J:D+=F
			if not M:
				D+=f"{O}"
				if not L:D+=N
			elif A>=16:D+=f" Calculating... "
			if not K:D+=C
			return D
		def print_progress_bar(iteration,total,prefix='',suffix=''):
			D=prefix;C=total;B=iteration;A=suffix;D+=' |'if not D.endswith(' |')else'';A=f"| {A}"if not A.startswith('| ')else A
			try:
				E,F=multiCMD.get_terminal_size();sys.stdout.write(f"\r{multiCMD._genrate_progress_bar(B,C,D,A,E)}");sys.stdout.flush()
				if B==C and C>0:print(file=sys.stdout)
			except:
				if B%5==0:print(multiCMD._genrate_progress_bar(B,C,D,A))
		def format_bytes(size,use_1024_bytes=None,to_int=False,to_str=False,str_format='.2f'):
			H=str_format;F=to_str;C=use_1024_bytes;A=size
			if to_int or isinstance(A,str):
				if isinstance(A,int):return A
				elif isinstance(A,str):
					K=re.match('(\\d+(\\.\\d+)?)\\s*([a-zA-Z]*)',A)
					if not K:
						if F:return A
						print("Invalid size format. Expected format: 'number [unit]', e.g., '1.5 GiB' or '1.5GiB'");print(f"Got: {A}");return 0
					G,L,D=K.groups();G=float(G);D=D.strip().lower().rstrip('b')
					if D.endswith('i'):C=True
					elif C is None:C=False
					D=D.rstrip('i')
					if C:B=2**10
					else:B=10**3
					I={'':0,'k':1,'m':2,'g':3,'t':4,'p':5,'e':6,'z':7,'y':8}
					if D not in I:
						if F:return A
					else:
						if F:return multiCMD.format_bytes(size=int(G*B**I[D]),use_1024_bytes=C,to_str=True,str_format=H)
						return int(G*B**I[D])
				else:
					try:return int(A)
					except Exception:return 0
			elif F or isinstance(A,int)or isinstance(A,float):
				if isinstance(A,str):
					try:A=A.rstrip('B').rstrip('b');A=float(A.lower().strip())
					except Exception:return A
				if C or C is None:
					B=2**10;E=0;J={0:'',1:'Ki',2:'Mi',3:'Gi',4:'Ti',5:'Pi',6:'Ei',7:'Zi',8:'Yi'}
					while A>B:A/=B;E+=1
					return f"{A:{H}} {' '}{J[E]}".replace('  ',' ')
				else:
					B=10**3;E=0;J={0:'',1:'K',2:'M',3:'G',4:'T',5:'P',6:'E',7:'Z',8:'Y'}
					while A>B:A/=B;E+=1
					return f"{A:{H}} {' '}{J[E]}".replace('  ',' ')
			else:
				try:return multiCMD.format_bytes(float(A),C)
				except Exception:pass
				return 0