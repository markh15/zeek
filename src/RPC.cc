// $Id: RPC.cc 6219 2008-10-01 05:39:07Z vern $
//
// See the file "COPYING" in the main distribution directory for copyright.

#include "config.h"

#include <stdlib.h>

#include "NetVar.h"
#include "XDR.h"
#include "RPC.h"
#include "Sessions.h"

namespace { // local namespace
	const bool DEBUG_rpc_resync = false;
}

// TODO: Should we add start_time and last_time to the rpc_* events??

// TODO: make this configurable
#define MAX_RPC_LEN 65536


RPC_CallInfo::RPC_CallInfo(uint32 arg_xid, const u_char*& buf, int& n, double arg_start_time, double arg_last_time, int arg_rpc_len)
	{
	xid = arg_xid;

	start_time = arg_start_time;
	last_time = arg_last_time;
	rpc_len = arg_rpc_len;
	call_n = n;
	call_buf = new u_char[call_n];
	memcpy((void*) call_buf, (const void*) buf, call_n);

	rpc_version = extract_XDR_uint32(buf, n);
	prog = extract_XDR_uint32(buf, n);
	vers = extract_XDR_uint32(buf, n);
	proc = extract_XDR_uint32(buf, n);
	cred_flavor = skip_XDR_opaque_auth(buf, n);
	verf_flavor = skip_XDR_opaque_auth(buf, n);

	header_len = call_n - n;

	valid_call = false;

	v = 0;
	/*GM cookie = 0; */
	}

RPC_CallInfo::~RPC_CallInfo()
	{
	delete [] call_buf;
	Unref(v);
	/*GM
	if (cookie)
		delete cookie;
	*/
	}

int RPC_CallInfo::CompareRexmit(const u_char* buf, int n) const
	{
	if ( n != call_n )
		return 0;

	return memcmp((const void*) call_buf, (const void*) buf, call_n) == 0;
	}


void rpc_callinfo_delete_func(void* v)
	{
	delete (RPC_CallInfo*) v;
	}

RPC_Interpreter::RPC_Interpreter(Analyzer* arg_analyzer)
	{
	analyzer = arg_analyzer;
	calls.SetDeleteFunc(rpc_callinfo_delete_func);
	}

RPC_Interpreter::~RPC_Interpreter()
	{
	}

int RPC_Interpreter::DeliverRPC(const u_char* buf, int n, int rpclen, int is_orig, double start_time, double last_time)
	{
	uint32 xid = extract_XDR_uint32(buf, n);
	uint32 msg_type = extract_XDR_uint32(buf, n);
	int rpc_len = n; 

	if ( ! buf )
		return 0;
	HashKey h(&xid, 1);
	RPC_CallInfo* call = calls.Lookup(&h);

	if ( msg_type == RPC_CALL )
		{
		if ( ! is_orig )
			Weird("responder_RPC_call");

		if ( call )
			{
			if ( ! call->CompareRexmit(buf, n) )
				Weird("RPC_rexmit_inconsistency");
			// XXX: Should we update start_time and last_time or not??
			call->SetStartTime(start_time);
			call->SetLastTime(last_time);

			// TODO: Not sure whether the handling if rexmit
			// inconsistencies are correct. Maybe we should use the info in the new 
			// call for further processing.
			if ( call->HeaderLen() > n )
				{
				Weird("RPC_underflow");
				return 0;
				}

			n -= call->HeaderLen();
			buf += call->HeaderLen();
			}

		else
			{
			call = new RPC_CallInfo(xid, buf, n, start_time, last_time, rpc_len);
			if ( ! buf )
				{
				Weird("bad_RPC");
				delete call;
				return 0;
				}

			calls.Insert(&h, call);
			}
		// We now have a valid RPC_CallInfo (either the previous one in case 
		// of a rexmit or the current one). 
		// TODO: What to do in case of a rexmit_inconistency?? 
		Event_RPC_Call(call);

		if ( RPC_BuildCall(call, buf, n) )
			call->SetValidCall();
		else
			{
			Weird("bad_RPC");
			return 0;
			}
		}

	else if ( msg_type == RPC_REPLY )
		{
		if ( is_orig )
			Weird("originator_RPC_reply");

		uint32 reply_stat = extract_XDR_uint32(buf, n);
		if ( ! buf )
			return 0;

		BroEnum::rpc_status status = BroEnum::RPC_UNKNOWN_ERROR;

		if ( reply_stat == RPC_MSG_ACCEPTED )
			{
			(void) skip_XDR_opaque_auth(buf, n);
			uint32 accept_stat = extract_XDR_uint32(buf, n);

			// The first members of BroEnum::RPC_* correspond
			// to accept_stat.
			if ( accept_stat <= RPC_SYSTEM_ERR )
				status = (BroEnum::rpc_status)accept_stat;

			if ( ! buf )
				return 0;

			if ( accept_stat == RPC_PROG_MISMATCH )
				{
				(void) extract_XDR_uint32(buf, n);
				(void) extract_XDR_uint32(buf, n);

				if ( ! buf )
					return 0;
				}
			}

		else if ( reply_stat == RPC_MSG_DENIED )
			{
			uint32 reject_stat = extract_XDR_uint32(buf, n);
			if ( ! buf )
				return 0;

			if ( reject_stat == RPC_MISMATCH )
				{
				// Note that RPC_MISMATCH == 0 == RPC_SUCCESS.
				status = BroEnum::RPC_VERS_MISMATCH;

				(void) extract_XDR_uint32(buf, n);
				(void) extract_XDR_uint32(buf, n);

				if ( ! buf )
					return 0;
				}

			else if ( reject_stat == RPC_AUTH_ERROR )
				{
				status = BroEnum::RPC_AUTH_ERROR;

				(void) extract_XDR_uint32(buf, n);
				if ( ! buf )
					return 0;
				}

			else
				{
				status = BroEnum::RPC_UNKNOWN_ERROR;
				Weird("bad_RPC");
				}
			}

		else
			Weird("bad_RPC");

		// We now have extracted the status we want to use. 
		Event_RPC_Reply(xid, status, n);


		if ( call )
			{
			if ( ! call->IsValidCall() )
				{
				if ( status == BroEnum::RPC_SUCCESS )
					Weird("successful_RPC_reply_to_invalid_request");
				// We can't process this further, even if
				// it was successful, because the call
				// info won't be fully set up.
				}

			else
				{
				if ( ! RPC_BuildReply(call, (BroEnum::rpc_status)status, buf, n, start_time, last_time, rpc_len) )
					Weird("bad_RPC");
				}

			Event_RPC_Dialogue(call, status, n);

			delete calls.RemoveEntry(&h);
			}
		else
			{
			Weird("unpaired_RPC_response");
			n = 0;
			}
		}

	else
		Weird("bad_RPC");

	if ( n > 0 )
		{
		// If it's just padded with zeroes, don't complain.
		for ( ; n > 0; --n, ++buf )
			if ( *buf != 0 )
				break;

		if ( n > 0 )
			Weird("excess_RPC");
		}

	else if ( n < 0 )
		internal_error("RPC underflow");

	return 1;
	}

void RPC_Interpreter::Timeout()
	{
	IterCookie* cookie = calls.InitForIteration();
	RPC_CallInfo* c;

	while ( (c = calls.NextEntry(cookie)) )
		{
		Event_RPC_Dialogue(c, BroEnum::RPC_TIMEOUT, 0);
		if ( c->IsValidCall() )
			{
			const u_char* buf;
			int n = 0;
			if ( ! RPC_BuildReply(c, BroEnum::RPC_TIMEOUT, buf, n, network_time, network_time, 0) )
				Weird("bad_RPC");
			}
		}
	}

void RPC_Interpreter::Event_RPC_Dialogue(RPC_CallInfo* c, BroEnum::rpc_status status, int reply_len)
	{
	if ( rpc_dialogue )
		{
		val_list* vl = new val_list;
		vl->append(analyzer->BuildConnVal());
		vl->append(new Val(c->Program(), TYPE_COUNT));
		vl->append(new Val(c->Version(), TYPE_COUNT));
		vl->append(new Val(c->Proc(), TYPE_COUNT));
		vl->append(new EnumVal(status, enum_rpc_status));
		vl->append(new Val(c->StartTime(), TYPE_TIME));
		vl->append(new Val(c->CallLen(), TYPE_COUNT));
		vl->append(new Val(reply_len, TYPE_COUNT));
		analyzer->ConnectionEvent(rpc_dialogue, vl);
		}
	}

void RPC_Interpreter::Event_RPC_Call(RPC_CallInfo* c)
	{
	if ( rpc_call )
		{
		val_list* vl = new val_list;
		vl->append(analyzer->BuildConnVal());
		vl->append(new Val(c->XID(), TYPE_COUNT));
		vl->append(new Val(c->Program(), TYPE_COUNT));
		vl->append(new Val(c->Version(), TYPE_COUNT));
		vl->append(new Val(c->Proc(), TYPE_COUNT));
		vl->append(new Val(c->CallLen(), TYPE_COUNT));
		analyzer->ConnectionEvent(rpc_call, vl);
		}
	}

void RPC_Interpreter::Event_RPC_Reply(uint32_t xid, BroEnum::rpc_status status, int reply_len)
	{
	if ( rpc_reply )
		{
		val_list* vl = new val_list;
		vl->append(analyzer->BuildConnVal());
		vl->append(new Val(xid, TYPE_COUNT));
		vl->append(new EnumVal(status, enum_rpc_status));
		vl->append(new Val(reply_len, TYPE_COUNT));
		analyzer->ConnectionEvent(rpc_reply, vl);
		}
	}

void RPC_Interpreter::Weird(const char* msg)
	{
	analyzer->Weird(msg);
	}


void RPC_Reasm_Buffer::Init(int64_t arg_maxsize, int64_t arg_expected) {
	if (buf)
		delete [] buf;
	expected = arg_expected;
	maxsize = arg_maxsize;
	fill = processed = 0;
	buf = new u_char[maxsize];
};

bool RPC_Reasm_Buffer::ConsumeChunk(const u_char*& data, int& len) 
	{
	// How many bytes to we want to process with this call? 
	// Either the all of the bytes available or the number of bytes
	// that we are still missing
	int64_t to_process = min( len, (expected-processed) );

	if (fill < maxsize) 
		{
		// We haven't yet filled the buffer.
		// How many bytes to copy into the buff. Either all of the bytes
		// we want to process or the number of bytes until we reach maxsize
		int64_t to_copy = min( to_process, (maxsize-fill) ); 
		if (to_copy)
			memcpy(buf+fill, data, to_copy);
		fill += to_copy;
		}
	processed += to_process;
	len -= to_process;
	data += to_process;
	return (expected == processed);
	}

Contents_RPC::Contents_RPC(Connection* conn, bool orig,
				RPC_Interpreter* arg_interp)
: TCP_SupportAnalyzer(AnalyzerTag::Contents_RPC, conn, orig)
	{
	interp = arg_interp;
	state = WAIT_FOR_MESSAGE;
	resync_state = INSYNC;
	resync_toskip = 0;
	start_time = 0;
	last_time = 0;
	}

void Contents_RPC::Init()
	{
	TCP_SupportAnalyzer::Init();

	TCP_Analyzer* tcp =
		static_cast<TCP_ApplicationAnalyzer*>(Parent())->TCP();
	assert(tcp);

	if ((IsOrig() ? tcp->OrigState() : tcp->RespState()) !=
						TCP_ENDPOINT_ESTABLISHED)
		{
		NeedResync();
		}
	}


Contents_RPC::~Contents_RPC()
	{
	}

void Contents_RPC::Undelivered(int seq, int len, bool orig)
	{
	TCP_SupportAnalyzer::Undelivered(seq, len, orig);
	NeedResync();
	}

bool Contents_RPC::CheckResync(int& len, const u_char*& data, bool orig)
	{
	uint32 frame_len;
	bool last_frag;
	uint32 xid;
	uint32 frame_type;

	bool discard_this_chunk = false;

	if (resync_state == INSYNC)
		return true;

	// This is an attempt to re-synchronize the stream with RPC
	// frames after a content gap.  
	// Returns true if we are in sync. 
	// Returns false otherwise (we are in resync mode)
	//
	// We try to look for the beginning of a RPC frame, assuming 
	// RPC frames begin at packet boundaries (though they may span 
	// over multiple packets) (note that the data* of DeliverStream()
	// usually starts at a packet boundrary). 
	//
	// If we see a frame start that makes sense (direction and
	// frame lenght seem ok), we try to read (skip over) the next RPC
	// message. If this is successfull and we the place we are seems 
	// like a valid start of a RPC msg (direction and frame length
	// seem ok). We assume that we have successfully resync'ed.
	
	// Assuming RPC frames align with packet boundaries ...

	while (len > 0)
		{
		if (resync_toskip) 
			{
			if ( DEBUG_rpc_resync )
				DEBUG_MSG("RPC resync: skipping %d bytes.\n", len);
			// We have some bytes to skip over. 
			if (resync_toskip < len)
				{
				len -= resync_toskip;
				data += resync_toskip;
				resync_toskip = 0;
				}
			else 
				{
				resync_toskip -= len;
				data += len;
				len = 0;
				return false;
				}
			}

		if (resync_toskip != 0)
			{
			// Should never happen
			internal_error("RPC resync: skipping over data failed");
			NeedResync();
			return false;
			}

		// Now lets see whether data points to the beginning of a
		// RPC frame. If the resync processs is successful, we should
		// be at the beginning of a frame.

		
		if ( len < 12 )
			{
			// Ignore small chunks. 
			if ( len != 1 && DEBUG_rpc_resync )
				{
				// One-byte fragments are likely caused by
				// TCP keep-alive retransmissions.
				DEBUG_MSG("%.6f RPC resync: "
						  "discard small pieces: %d\n",
							  network_time, len);
				Conn()->Weird(
					fmt("RPC resync: discard %d bytes\n",
						len));
				}
			NeedResync();
			return false;
			}


		const u_char *xdata = data;
		int xlen = len;
		frame_len = extract_XDR_uint32(xdata, xlen);
		last_frag = (frame_len & 0x80000000) != 0;
		frame_len &= 0x7fffffff;
		xid = extract_XDR_uint32(xdata, xlen);
		frame_type = extract_XDR_uint32(xdata, xlen);

		// Check if the direction makes sense and the length of the
		// frame to expect.
		if ( (IsOrig() && frame_type != 0) ||
			 (! IsOrig() && frame_type != 1) ||
			 frame_len < 16 )

			discard_this_chunk = true;

		// Make sure the frame isn't too long. 
		// TODO: Could possible even reduce this number even further. 
		if (frame_len > MAX_RPC_LEN)
			discard_this_chunk = true;

		if (discard_this_chunk)
			{
			// Skip this chunk
			if ( DEBUG_rpc_resync )
				DEBUG_MSG("RPC resync: Need to resync. dicarding %d bytes.\n", len);
			NeedResync();  // let's try the resync again from the beginning
			return false;
			}

		// Looks like we are at the start of a frame and have successfully 
		// extracted the frame length (marker). 

		switch (resync_state) {
		case NEED_RESYNC:
		case RESYNC_WAIT_FOR_MSG_START:
			// Initial phase of resyncing. Skip frames until we get a frame
			// with the last_fragment bit set.
			resync_toskip = frame_len + 4;
			if (last_frag)
				resync_state = RESYNC_WAIT_FOR_FULL_MSG;
			else 
				resync_state = RESYNC_WAIT_FOR_MSG_START;
			break;

		case RESYNC_WAIT_FOR_FULL_MSG:
			// If the resync was successful so far, we should now be the start 
			// of a new RPC message. Try to skip over it.
			resync_toskip = frame_len + 4;
			if (last_frag)
				resync_state = RESYNC_HAD_FULL_MSG;
			break;
			
		case RESYNC_HAD_FULL_MSG:
			// We have now successfully skipped over a full RPC message. 
			// If we got that far, we are in sync. 
			resync_state = INSYNC;
			if ( DEBUG_rpc_resync )
				DEBUG_MSG("RPC resync: success.\n");
			return true;

		default:
			// Shoult never happen
			NeedResync();
			return false;
		} // end switch
		} // end while (len>0)

	return false;
	}
	



void Contents_RPC::DeliverStream(int len, const u_char* data, bool orig)
	{
	TCP_SupportAnalyzer::DeliverStream(len, data, orig);
	uint32 marker;
	bool last_frag;

	if (!CheckResync(len, data, orig))
		return;   // Not in sync yet. Still resyncing



	// Should be in sync now

	while (len > 0) 
		{
		last_time = network_time;
		switch (state) {
		case WAIT_FOR_MESSAGE: 
			// A new RPC message is starting. Initialize state

			// We expect and want 4 bytes of the frame markers
			marker_buf.Init(4,4);
			// We want at most 64KB of message data and we don't know
			// yet how much we expect, so we set expected to 0
			msg_buf.Init(MAX_RPC_LEN, 0);
			last_frag = 0;
			state = WAIT_FOR_MARKER;
			start_time = network_time;
			// no break. fall through
			
		case WAIT_FOR_MARKER:
			{
			bool got_marker = marker_buf.ConsumeChunk(data,len);
			if (got_marker)
				{
				const u_char *dummy_p = marker_buf.GetBuf();
				int dummy_len = (int) marker_buf.GetFill();
				// have full marker
				marker = extract_XDR_uint32(dummy_p, dummy_len);
				marker_buf.Init(4,4);
				if ( ! dummy_p ) 
					{
					internal_error("inconsistent RPC record marker extraction");
					}

				last_frag = (marker & 0x80000000) != 0;
				marker &= 0x7fffffff;
					//printf("%.6f %d marker= %u <> last_frag= %d <> expected=%llu <> processed= %llu <> len = %d\n",
					//		network_time, IsOrig(), marker, last_frag, msg_buf.GetExpected(), msg_buf.GetProcessed(), len);
				if (!msg_buf.AddToExpected(marker))
					Conn()->Weird(fmt("RPC_message_too_long (%" PRId64 ")" , msg_buf.GetExpected()));
				if (last_frag)
					state = WAIT_FOR_LAST_DATA;
				else
					state = WAIT_FOR_DATA;
				}
			}
			// else remain in state. Haven't got the full 4 bytes for the marker yet
			break;

		case WAIT_FOR_DATA:
		case WAIT_FOR_LAST_DATA:
			{
			bool got_all_data = msg_buf.ConsumeChunk(data, len);
			if (got_all_data) 
				{
				// got all the data we expected. Now let's see whether there is 
				// another fragment coming or whether we just finished the last
				// fragment. 
				if (state == WAIT_FOR_LAST_DATA) 
					{
					const u_char *dummy_p = msg_buf.GetBuf();
					int dummy_len = (int) msg_buf.GetFill();
					if ( ! interp->DeliverRPC(dummy_p, dummy_len, (int)msg_buf.GetExpected(), IsOrig(), start_time, last_time) )
						Conn()->Weird("partial_RPC");
					state = WAIT_FOR_MESSAGE;
					}
				else
					state = WAIT_FOR_MARKER;
				}
			// else remain in state. Haven't read all the data yet.
			}
			break;
		} // end switch 
		} // end while
	}

RPC_Analyzer::RPC_Analyzer(AnalyzerTag::Tag tag, Connection* conn,
				RPC_Interpreter* arg_interp)
: TCP_ApplicationAnalyzer(tag, conn)
	{
	interp = arg_interp;

	if ( Conn()->ConnTransport() == TRANSPORT_UDP )
		ADD_ANALYZER_TIMER(&RPC_Analyzer::ExpireTimer,
			network_time + rpc_timeout, 1, TIMER_RPC_EXPIRE);
	}

RPC_Analyzer::~RPC_Analyzer()
	{
	delete interp;
	}

void RPC_Analyzer::DeliverPacket(int len, const u_char* data, bool orig,
					int seq, const IP_Hdr* ip, int caplen)
	{
	TCP_ApplicationAnalyzer::DeliverPacket(len, data, orig, seq, ip, caplen);
	len = min(len, caplen);

	if ( orig )
		{
		if ( ! interp->DeliverRPC(data, len, len, 1, network_time, network_time) )
			Weird("bad_RPC");
		}
	else
		{
		if ( ! interp->DeliverRPC(data, len, len, 0, network_time, network_time) )
			Weird("bad_RPC");
		}
	}

void RPC_Analyzer::Done()
	{
	TCP_ApplicationAnalyzer::Done();

	interp->Timeout();
#if 0
TODO: maybe put this check back in. But there are so many other 
things the RPC analyzer might miss....
	// This code was replicated in NFS.cc and Portmap.cc, so we factor
	// it into here.  The semantics have slightly changed - it used
	// to be we'd always execute interp->Timeout(), but now we only
	// do for UDP.

	if ( Conn()->ConnTransport() == TRANSPORT_TCP && TCP() )
		{
		
		if ( orig_rpc->State() != RPC_COMPLETE &&
		     (TCP()->OrigState() == TCP_ENDPOINT_CLOSED ||
		      TCP()->OrigPrevState() == TCP_ENDPOINT_CLOSED) &&
		     // Sometimes things like tcpwrappers will immediately
		     // close the connection, without any data having been
		     // transferred.  Don't bother flagging these.
		     TCP()->Orig()->Size() > 0 )
			Weird("partial_RPC_request");
		}
	else
		interp->Timeout();
#endif
	}

void RPC_Analyzer::ExpireTimer(double /* t */)
	{
	Event(connection_timeout);
	sessions->Remove(Conn());
	}

// The binpac version of interpreter.
#include "rpc_pac.h"

RPC_UDP_Analyzer_binpac::RPC_UDP_Analyzer_binpac(Connection* conn)
: Analyzer(AnalyzerTag::RPC_UDP_BINPAC, conn)
	{
	interp = new binpac::SunRPC::RPC_Conn(this);
	ADD_ANALYZER_TIMER(&RPC_UDP_Analyzer_binpac::ExpireTimer,
			network_time + rpc_timeout, 1, TIMER_RPC_EXPIRE);
	}

RPC_UDP_Analyzer_binpac::~RPC_UDP_Analyzer_binpac()
	{
	delete interp;
	}

void RPC_UDP_Analyzer_binpac::Done()
	{
	Analyzer::Done();
	interp->Timeout();
	}

void RPC_UDP_Analyzer_binpac::DeliverPacket(int len, const u_char* data, bool orig, int seq, const IP_Hdr* ip, int caplen)
	{
	Analyzer::DeliverPacket(len, data, orig, seq, ip, caplen);
	try
		{
		interp->NewData(orig, data, data + len);
		}
	catch ( binpac::Exception &e )
		{
		Weird(fmt("bad_RPC: %s", e.msg().c_str()));
		}
	}

void RPC_UDP_Analyzer_binpac::ExpireTimer(double /* t */)
	{
	Event(connection_timeout);
	sessions->Remove(Conn());
	}
