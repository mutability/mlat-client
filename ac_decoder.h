#ifndef __AC_DECODER_INCLUDE__
#define __AC_DECODER_INCLUDE__
#include <map>

#include <time.h>
#include "ending.h"

//A模式，应答机编码
#define AC_MODE_A	1
//C模式高度编码
#define AC_MODE_C	2
//无法区分
#define AC_MODE_NA	0
//无效的高度
#define AC_INVALID_ALTITUDE -1

typedef struct ac_decode_result
{
	int type ;
	unsigned short squawk ;
	bool is_spi ; 
	int altitude ; 
}ac_decode_result_t ;


class ac_decoder
{
public:
	ac_decoder() ; 
	ac_decode_result_t decode(unsigned char ac[2]) ; 
private:
	typedef struct ac_count_stat_item
	{
		int counted;
		int counting ;
	}ac_count_stat_item_t;
	typedef std::map<unsigned short ,ac_count_stat_item_t>	ac_count_stat_t ; 
private:
	int get_ac_type(unsigned short modeac) ;
	int get_mode_count_stat(ac_count_stat_t& which , unsigned short modea);
	void inc_mode_stat(ac_count_stat_t& which ,unsigned short modea);
	void commit_ac_mode_stat();
	int modeA2modeC(unsigned int modea) ;

private:
	time_t next_check_time ;
	const  int check_stat_interval ; 
	ac_count_stat_t a_mode_stat ;
	ac_count_stat_t	na_mode_stat ; 
} ;

#endif
