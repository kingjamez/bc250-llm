#!/usr/bin/env bash
# bc250-gpu-stress.sh - continuous Vulkan compute load for BC-250 thermal/OC testing.
# Submits back-to-back compute dispatches (no CPU verification) so the GPU stays
# pegged at ~100% for the whole duration. Derived from cv.sh's Vulkan setup.
# Run inside the umrbox distrobox (needs glslangValidator, gcc, libvulkan).
set -euo pipefail

ELEMENTS=16777216   # multiple of 256; 4 buffers if changed (here 3 x 64MB)
ITERS=512           # inner ALU loop length per element per dispatch
DURATION=480        # seconds to sustain load
VERIFY=0            # 1 = fixed-seed + emit deterministic output checksum (correctness)

while [ "$#" -gt 0 ]; do
	case "$1" in
		--elements) ELEMENTS="$2"; shift 2;;
		--iters)    ITERS="$2";    shift 2;;
		--duration) DURATION="$2"; shift 2;;
		--verify)   VERIFY=1;      shift;;
		-h|--help)  echo "usage: $0 [--elements N] [--iters N] [--duration SEC] [--verify]"; exit 0;;
		*) echo "unknown arg: $1" >&2; exit 2;;
	esac
done

command -v glslangValidator >/dev/null || { echo "ERROR: glslangValidator not found (run inside umrbox)" >&2; exit 1; }
command -v gcc >/dev/null || { echo "ERROR: gcc not found (run inside umrbox)" >&2; exit 1; }

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

cat >"$TMPDIR/s.comp" <<'GLSL'
#version 450
layout(local_size_x = 256) in;
layout(std430, set = 0, binding = 0) readonly  buffer A { uint a[]; };
layout(std430, set = 0, binding = 1) readonly  buffer B { uint b[]; };
layout(std430, set = 0, binding = 2) writeonly buffer O { uint o[]; };
layout(push_constant) uniform P { uint n; uint seed; uint iters; } pc;
shared uint lds[256];
uint rotl(uint v, uint s) { s &= 31u; return s == 0u ? v : ((v << s) | (v >> (32u - s))); }
void main() {
	uint idx = gl_GlobalInvocationID.x;
	uint lid = gl_LocalInvocationID.x;
	uint x = a[idx] ^ pc.seed;
	uint y = b[idx] + rotl(idx ^ pc.seed, 7u);
	float f = uintBitsToFloat(0x3f800000u | (x & 0x007fffffu));
	for (uint j = 0u; j < pc.iters; ++j) {
		x = x * 1664525u + 1013904223u + j;
		x ^= rotl(y + j * 0x45d9f3bu, j);
		y += x ^ (j * 0x27d4eb2du) ^ (x >> ((j & 7u) + 1u));
		f = fma(f, 1.0009765625, float(int(y & 255u) - 128) * 0.00000011920928955078125);
	}
	lds[lid] = x ^ y ^ pc.seed;
	barrier();
	uint p0 = lds[(lid * 17u) & 255u];
	uint p1 = lds[(lid + 1u) & 255u];
	x ^= p0 + rotl(p1, lid);
	y ^= rotl(p0 ^ p1, 11u);
	o[idx] = x ^ y ^ floatBitsToUint(f);
}
GLSL

cat >"$TMPDIR/s.c" <<'C'
#define _POSIX_C_SOURCE 200809L
#include <vulkan/vulkan.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#define LOCAL 256u
#define CHECK(c) do { VkResult _r=(c); if(_r){fprintf(stderr,"%s=%d L%d\n",#c,_r,__LINE__);return 1;} } while(0)
struct params { uint32_t n, seed, iters; };
static uint32_t findmem(VkPhysicalDevice pd, uint32_t bits, VkMemoryPropertyFlags fl) {
	VkPhysicalDeviceMemoryProperties p; vkGetPhysicalDeviceMemoryProperties(pd,&p);
	for (uint32_t i=0;i<p.memoryTypeCount;i++)
		if ((bits&(1u<<i)) && (p.memoryTypes[i].propertyFlags&fl)==fl) return i;
	return UINT32_MAX;
}
static double nows(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static int readf(const char*path,char**buf,size_t*sz){
	FILE*f=fopen(path,"rb"); if(!f)return 1; fseek(f,0,SEEK_END); long n=ftell(f); rewind(f);
	if(n<=0){fclose(f);return 1;} *buf=malloc((size_t)n);
	if(fread(*buf,1,(size_t)n,f)!=(size_t)n){fclose(f);free(*buf);return 1;} fclose(f); *sz=(size_t)n; return 0;
}
int main(int argc,char**argv){
	if(argc!=5&&argc!=6){fprintf(stderr,"usage: %s spv elements iters duration [verify]\n",argv[0]);return 2;}
	const char*spv_path=argv[1];
	uint32_t n=(uint32_t)strtoul(argv[2],0,0), iters=(uint32_t)strtoul(argv[3],0,0);
	double duration=atof(argv[4]);
	int verify=(argc==6)?atoi(argv[5]):0;
	if(!n||(n%LOCAL)){fprintf(stderr,"elements must be multiple of 256\n");return 2;}
	VkDeviceSize bytes=(VkDeviceSize)n*4;
	VkApplicationInfo app={.sType=VK_STRUCTURE_TYPE_APPLICATION_INFO,.apiVersion=VK_API_VERSION_1_1};
	VkInstanceCreateInfo ici={.sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,.pApplicationInfo=&app};
	VkInstance inst; CHECK(vkCreateInstance(&ici,0,&inst));
	VkPhysicalDevice pds[16]; uint32_t pdc=16; CHECK(vkEnumeratePhysicalDevices(inst,&pdc,pds));
	VkPhysicalDevice pd=VK_NULL_HANDLE; VkPhysicalDeviceProperties pp;
	for(uint32_t i=0;i<pdc;i++){vkGetPhysicalDeviceProperties(pds[i],&pp); if(pp.vendorID==0x1002){pd=pds[i];break;}}
	if(pd==VK_NULL_HANDLE){fprintf(stderr,"no AMD GPU\n");return 1;}
	vkGetPhysicalDeviceProperties(pd,&pp);
	uint32_t qf=UINT32_MAX,qc=32; VkQueueFamilyProperties qp[32];
	vkGetPhysicalDeviceQueueFamilyProperties(pd,&qc,qp);
	for(uint32_t i=0;i<qc;i++) if(qp[i].queueFlags&VK_QUEUE_COMPUTE_BIT){qf=i;break;}
	if(qf==UINT32_MAX){fprintf(stderr,"no compute queue\n");return 1;}
	float prio=1; VkDeviceQueueCreateInfo qci={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=qf,.queueCount=1,.pQueuePriorities=&prio};
	VkDeviceCreateInfo dci={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&qci};
	VkDevice dev; CHECK(vkCreateDevice(pd,&dci,0,&dev));
	VkQueue q; vkGetDeviceQueue(dev,qf,0,&q);
	VkBuffer buf[3]; VkDeviceMemory mem[3]; void*map[3];
	for(int i=0;i<3;i++){
		VkBufferCreateInfo bci={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=bytes,.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE};
		CHECK(vkCreateBuffer(dev,&bci,0,&buf[i]));
		VkMemoryRequirements rq; vkGetBufferMemoryRequirements(dev,buf[i],&rq);
		uint32_t mt=findmem(pd,rq.memoryTypeBits,VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
		if(mt==UINT32_MAX){fprintf(stderr,"no host-visible mem\n");return 1;}
		VkMemoryAllocateInfo mai={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.allocationSize=rq.size,.memoryTypeIndex=mt};
		CHECK(vkAllocateMemory(dev,&mai,0,&mem[i])); CHECK(vkBindBufferMemory(dev,buf[i],mem[i],0));
		CHECK(vkMapMemory(dev,mem[i],0,bytes,0,&map[i]));
	}
	for(uint32_t i=0;i<n;i++){((uint32_t*)map[0])[i]=i*17u+3u;((uint32_t*)map[1])[i]=i^0x9e3779b9u;((uint32_t*)map[2])[i]=0;}
	VkDescriptorSetLayoutBinding bnd[3];
	for(int i=0;i<3;i++) bnd[i]=(VkDescriptorSetLayoutBinding){.binding=(uint32_t)i,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT};
	VkDescriptorSetLayoutCreateInfo dsli={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,.bindingCount=3,.pBindings=bnd};
	VkDescriptorSetLayout dsl; CHECK(vkCreateDescriptorSetLayout(dev,&dsli,0,&dsl));
	VkPushConstantRange pcr={.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT,.size=sizeof(struct params)};
	VkPipelineLayoutCreateInfo plci={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,.setLayoutCount=1,.pSetLayouts=&dsl,.pushConstantRangeCount=1,.pPushConstantRanges=&pcr};
	VkPipelineLayout pl; CHECK(vkCreatePipelineLayout(dev,&plci,0,&pl));
	char*spv;size_t spvs; if(readf(spv_path,&spv,&spvs)){fprintf(stderr,"read spv failed\n");return 1;}
	VkShaderModuleCreateInfo smci={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,.codeSize=spvs,.pCode=(const uint32_t*)spv};
	VkShaderModule sm; CHECK(vkCreateShaderModule(dev,&smci,0,&sm));
	VkComputePipelineCreateInfo cpci={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,.stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,.stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=sm,.pName="main"},.layout=pl};
	VkPipeline pipe; CHECK(vkCreateComputePipelines(dev,VK_NULL_HANDLE,1,&cpci,0,&pipe));
	VkDescriptorPoolSize psz={.type=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=3};
	VkDescriptorPoolCreateInfo dpci={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,.maxSets=1,.poolSizeCount=1,.pPoolSizes=&psz};
	VkDescriptorPool dp; CHECK(vkCreateDescriptorPool(dev,&dpci,0,&dp));
	VkDescriptorSetAllocateInfo dsai={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,.descriptorPool=dp,.descriptorSetCount=1,.pSetLayouts=&dsl};
	VkDescriptorSet dset; CHECK(vkAllocateDescriptorSets(dev,&dsai,&dset));
	for(int i=0;i<3;i++){
		VkDescriptorBufferInfo dbi={.buffer=buf[i],.range=bytes};
		VkWriteDescriptorSet w={.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=dset,.dstBinding=(uint32_t)i,.descriptorCount=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.pBufferInfo=&dbi};
		vkUpdateDescriptorSets(dev,1,&w,0,0);
	}
	VkCommandPoolCreateInfo cmpci={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.flags=VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT,.queueFamilyIndex=qf};
	VkCommandPool cmdp; CHECK(vkCreateCommandPool(dev,&cmpci,0,&cmdp));
	VkCommandBufferAllocateInfo cbai={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=cmdp,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
	VkCommandBuffer cmd; CHECK(vkAllocateCommandBuffers(dev,&cbai,&cmd));
	VkFenceCreateInfo fci={.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
	VkFence fence; CHECK(vkCreateFence(dev,&fci,0,&fence));
	printf("device=%s stress elements=%u iters=%u duration=%.0fs\n",pp.deviceName,n,iters,duration); fflush(stdout);
	double start=nows(), last=start; uint64_t cnt=0;
	while(nows()-start<duration){
		struct params p={.n=n,.seed=verify?0xa5a5a5a5u:(0xa5a5a5a5u^(uint32_t)cnt),.iters=iters};
		VkCommandBufferBeginInfo bi={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
		CHECK(vkResetCommandBuffer(cmd,0));
		CHECK(vkBeginCommandBuffer(cmd,&bi));
		vkCmdBindPipeline(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,pipe);
		vkCmdBindDescriptorSets(cmd,VK_PIPELINE_BIND_POINT_COMPUTE,pl,0,1,&dset,0,0);
		vkCmdPushConstants(cmd,pl,VK_SHADER_STAGE_COMPUTE_BIT,0,sizeof(p),&p);
		vkCmdDispatch(cmd,n/LOCAL,1,1);
		CHECK(vkEndCommandBuffer(cmd));
		VkSubmitInfo si={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&cmd};
		CHECK(vkQueueSubmit(q,1,&si,fence));
		CHECK(vkWaitForFences(dev,1,&fence,VK_TRUE,UINT64_MAX));
		CHECK(vkResetFences(dev,1,&fence));
		cnt++;
		double t=nows();
		if(t-last>=5.0){ printf("  t=%.0fs dispatches=%llu rate=%.1f/s\n",t-start,(unsigned long long)cnt,cnt/(t-start)); fflush(stdout); last=t; }
	}
	printf("done dispatches=%llu in %.0fs\n",(unsigned long long)cnt,nows()-start);
	if(verify){
		const uint32_t*o=(const uint32_t*)map[2];
		uint64_t ck=1469598103934665603ULL;          /* FNV-1a over the output buffer */
		for(uint32_t i=0;i<n;i++){ ck^=o[i]; ck*=1099511628211ULL; }
		printf("verify_checksum=0x%016llx\n",(unsigned long long)ck);
	}
	fflush(stdout);
	return 0;
}
C

echo "Compiling GPU stress..."
glslangValidator -V "$TMPDIR/s.comp" -o "$TMPDIR/s.spv" >/dev/null
gcc -std=c11 -O2 -Wall -o "$TMPDIR/s" "$TMPDIR/s.c" -lvulkan -lm
echo "Running GPU stress (${DURATION}s, ${ELEMENTS} elements, ${ITERS} iters, verify=${VERIFY})..."
"$TMPDIR/s" "$TMPDIR/s.spv" "$ELEMENTS" "$ITERS" "$DURATION" "$VERIFY"
