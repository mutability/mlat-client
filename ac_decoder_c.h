#ifndef __AC_DECODER_INCLUDE_C_INCLUDE_
#define __AC_DECODER_INCLUDE_C_INCLUDE_

#ifdef __cplusplus
extern "C" {
#endif

typedef struct ac_decode_resultd
{
	int type ;
	unsigned short squawk ;
	int  is_spi ;
	int altitude ;
}ac_decode_result_t ;

#define AC_MODE_A	1
#define AC_MODE_C	2
#define AC_MODE_NA	0
#define AC_INVALID_ALTITUDE -1


ac_decode_result_t  ac_decode(unsigned char ac[2]) ;

#ifdef __cplusplus
}
#endif

#endif
